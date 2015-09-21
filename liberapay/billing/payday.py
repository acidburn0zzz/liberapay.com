from __future__ import unicode_literals

import aspen.utils
from aspen import log
from psycopg2 import IntegrityError

from liberapay.billing.exchanges import transfer
from liberapay.exceptions import NegativeBalance
from liberapay.models import check_db


with open('sql/fake_payday.sql') as f:
    FAKE_PAYDAY = f.read()


class NoPayday(Exception):
    __str__ = lambda self: "No payday found where one was expected."


class Payday(object):

    @classmethod
    def start(cls):
        """Try to start a new Payday.

        If there is a Payday that hasn't finished yet, then the UNIQUE
        constraint on ts_end will kick in and notify us of that. In that case
        we load the existing Payday and work on it some more. We use the start
        time of the current Payday to synchronize our work.

        """
        try:
            d = cls.db.one("""
                INSERT INTO paydays DEFAULT VALUES
                RETURNING id, (ts_start AT TIME ZONE 'UTC') AS ts_start
            """, back_as=dict)
            log("Starting a new payday.")
        except IntegrityError:  # Collision, we have a Payday already.
            d = cls.db.one("""
                SELECT id, (ts_start AT TIME ZONE 'UTC') AS ts_start
                  FROM paydays
                 WHERE ts_end='1970-01-01T00:00:00+00'::timestamptz
            """, back_as=dict)
            log("Picking up with an existing payday.")

        d['ts_start'] = d['ts_start'].replace(tzinfo=aspen.utils.utc)

        log("Payday started at %s." % d['ts_start'])

        payday = Payday()
        payday.__dict__.update(d)
        return payday

    def run(self):
        """This is the starting point for payday.

        This method runs every Thursday. It is structured such that it can be
        run again safely (with a newly-instantiated Payday object) if it
        crashes.

        """
        self.db.self_check()

        _start = aspen.utils.utcnow()
        log("Greetings, program! It's PAYDAY!!!!")

        self.shuffle()

        self.update_stats()
        self.update_cached_amounts()

        self.end()
        self.notify_participants()

        _end = aspen.utils.utcnow()
        _delta = _end - _start
        fmt_past = "Script ran for %%(age)s (%s)." % _delta
        log(aspen.utils.to_age(_start, fmt_past=fmt_past))

    def shuffle(self):
        with self.db.get_cursor() as cursor:
            self.prepare(cursor, self.ts_start)
            self.transfer_virtually(cursor, self.ts_start)
            transfers = cursor.all("""
                SELECT t.*
                     , p.mangopay_user_id AS tipper_mango_id
                     , p2.mangopay_user_id AS tippee_mango_id
                     , p.mangopay_wallet_id AS tipper_wallet_id
                     , p2.mangopay_wallet_id AS tippee_wallet_id
                  FROM payday_transfers t
                  JOIN participants p ON p.id = t.tipper
                  JOIN participants p2 ON p2.id = t.tippee
            """)
            try:
                self.check_balances(cursor)
                self.transfer_for_real(transfers)
                check_db(cursor)
            except:
                # Dump transfers for debugging
                import csv
                from time import time
                with open('%s_transfers.csv' % time(), 'wb') as f:
                    csv.writer(f).writerows(transfers)
                raise

        # Clean up leftover functions
        self.db.run("""
            DROP FUNCTION process_take();
            DROP FUNCTION process_tip();
            DROP FUNCTION settle_tip_graph();
            DROP FUNCTION transfer(bigint, bigint, numeric, transfer_context);
        """)

    @staticmethod
    def prepare(cursor, ts_start):
        """Prepare the DB: we need temporary tables with indexes and triggers.
        """
        cursor.run("""

        -- Create the necessary temporary tables and indexes

        CREATE TEMPORARY TABLE payday_participants ON COMMIT DROP AS
            SELECT id
                 , username
                 , join_time
                 , balance AS old_balance
                 , balance AS new_balance
                 , is_suspicious
                 , goal
              FROM participants p
             WHERE is_suspicious IS NOT true
               AND join_time < %(ts_start)s
               AND mangopay_user_id IS NOT NULL
          ORDER BY join_time;

        CREATE UNIQUE INDEX ON payday_participants (id);
        CREATE UNIQUE INDEX ON payday_participants (username);

        CREATE TEMPORARY TABLE payday_transfers_done ON COMMIT DROP AS
            SELECT *
              FROM transfers t
             WHERE t.timestamp > %(ts_start)s;

        CREATE TEMPORARY TABLE payday_tips ON COMMIT DROP AS
            SELECT tipper, tippee, amount
              FROM ( SELECT DISTINCT ON (tipper, tippee) *
                       FROM tips
                      WHERE mtime < %(ts_start)s
                   ORDER BY tipper, tippee, mtime DESC
                   ) t
              JOIN payday_participants p ON p.id = t.tipper
              JOIN payday_participants p2 ON p2.id = t.tippee
             WHERE t.amount > 0
               AND (p2.goal IS NULL or p2.goal >= 0)
               AND ( SELECT id
                       FROM payday_transfers_done t2
                      WHERE t.tipper = t2.tipper
                        AND t.tippee = t2.tippee
                        AND context = 'tip'
                   ) IS NULL
          ORDER BY p.join_time ASC, t.ctime ASC;

        CREATE INDEX ON payday_tips (tipper);
        CREATE INDEX ON payday_tips (tippee);
        ALTER TABLE payday_tips ADD COLUMN is_funded boolean;

        ALTER TABLE payday_participants ADD COLUMN giving_today numeric(35,2);
        UPDATE payday_participants p
           SET giving_today = COALESCE((
                   SELECT sum(amount)
                     FROM payday_tips
                    WHERE tipper = p.id
               ), 0);

        CREATE TEMPORARY TABLE payday_takes
        ( team bigint
        , member bigint
        , amount numeric(35,2)
        ) ON COMMIT DROP;

        CREATE TEMPORARY TABLE payday_transfers
        ( timestamp timestamptz DEFAULT now()
        , tipper bigint
        , tippee bigint
        , amount numeric(35,2)
        , context transfer_context
        , UNIQUE (tipper, tippee, context)
        ) ON COMMIT DROP;


        -- Prepare a statement that makes and records a transfer

        CREATE OR REPLACE FUNCTION transfer(bigint, bigint, numeric, transfer_context)
        RETURNS void AS $$
            BEGIN
                IF ($3 = 0) THEN RETURN; END IF;
                UPDATE payday_participants
                   SET new_balance = (new_balance - $3)
                 WHERE id = $1;
                IF (NOT FOUND) THEN RAISE 'tipper not found'; END IF;
                UPDATE payday_participants
                   SET new_balance = (new_balance + $3)
                 WHERE id = $2;
                IF (NOT FOUND) THEN RAISE 'tippee not found'; END IF;
                INSERT INTO payday_transfers
                            (tipper, tippee, amount, context)
                     VALUES ($1, $2, $3, $4);
            END;
        $$ LANGUAGE plpgsql;


        -- Create a trigger to process tips

        CREATE OR REPLACE FUNCTION process_tip() RETURNS trigger AS $$
            DECLARE
                tipper payday_participants;
            BEGIN
                tipper := (
                    SELECT p.*::payday_participants
                      FROM payday_participants p
                     WHERE id = NEW.tipper
                );
                IF (NEW.amount <= tipper.new_balance) THEN
                    EXECUTE transfer(NEW.tipper, NEW.tippee, NEW.amount, 'tip');
                    RETURN NEW;
                END IF;
                RETURN NULL;
            END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER process_tip BEFORE UPDATE OF is_funded ON payday_tips
            FOR EACH ROW
            WHEN (NEW.is_funded IS true AND OLD.is_funded IS NOT true)
            EXECUTE PROCEDURE process_tip();


        -- Create a trigger to process takes

        CREATE OR REPLACE FUNCTION process_take() RETURNS trigger AS $$
            DECLARE
                actual_amount numeric(35,2);
                team_balance numeric(35,2);
            BEGIN
                team_balance := (
                    SELECT new_balance
                      FROM payday_participants
                     WHERE id = NEW.team
                );
                IF (team_balance <= 0) THEN RETURN NULL; END IF;
                actual_amount := NEW.amount;
                IF (team_balance < NEW.amount) THEN
                    actual_amount := team_balance;
                END IF;
                EXECUTE transfer(NEW.team, NEW.member, actual_amount, 'take');
                RETURN NULL;
            END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER process_take AFTER INSERT ON payday_takes
            FOR EACH ROW EXECUTE PROCEDURE process_take();


        -- Create a function to settle whole tip graph

        CREATE OR REPLACE FUNCTION settle_tip_graph() RETURNS void AS $$
            DECLARE
                count integer NOT NULL DEFAULT 0;
                i integer := 0;
            BEGIN
                LOOP
                    i := i + 1;
                    WITH updated_rows AS (
                         UPDATE payday_tips
                            SET is_funded = true
                          WHERE is_funded IS NOT true
                      RETURNING *
                    )
                    SELECT COUNT(*) FROM updated_rows INTO count;
                    IF (count = 0) THEN
                        EXIT;
                    END IF;
                    IF (i > 50) THEN
                        RAISE 'Reached the maximum number of iterations';
                    END IF;
                END LOOP;
            END;
        $$ LANGUAGE plpgsql;


        -- Save the stats we already have

        UPDATE paydays
           SET nparticipants = (SELECT count(*) FROM payday_participants)
         WHERE ts_end='1970-01-01T00:00:00+00'::timestamptz;

        """, dict(ts_start=ts_start))
        log('Prepared the DB.')

    @staticmethod
    def transfer_virtually(cursor, ts_start):
        cursor.run("""

        SELECT settle_tip_graph();

        INSERT INTO payday_takes
            SELECT team, member, amount
              FROM ( SELECT DISTINCT ON (team, member)
                            team, member, amount, ctime
                       FROM takes
                      WHERE mtime < %(ts_start)s
                   ORDER BY team, member, mtime DESC
                   ) t
             WHERE t.amount > 0
               AND t.team IN (SELECT id FROM payday_participants)
               AND t.member IN (SELECT id FROM payday_participants)
               AND ( SELECT id
                       FROM payday_transfers_done t2
                      WHERE t.team = t2.tipper
                        AND t.member = t2.tippee
                        AND context = 'take'
                   ) IS NULL
          ORDER BY t.team, t.ctime DESC;

        SELECT settle_tip_graph();

        """, dict(ts_start=ts_start))

    @staticmethod
    def check_balances(cursor):
        """Check that balances aren't becoming (more) negative
        """
        oops = cursor.one("""
            SELECT *
              FROM (
                     SELECT p.id
                          , p.username
                          , (p.balance + p2.new_balance - p2.old_balance) AS new_balance
                          , p.balance AS cur_balance
                       FROM payday_participants p2
                       JOIN participants p ON p.id = p2.id
                        AND p2.new_balance <> p2.old_balance
                   ) foo
             WHERE new_balance < 0 AND new_balance < cur_balance
             LIMIT 1
        """)
        if oops:
            log(oops)
            raise NegativeBalance()
        log("Checked the balances.")

    def transfer_for_real(self, transfers):
        db = self.db
        for t in transfers:
            transfer(db, **t._asdict())

    def update_stats(self):
        self.db.run("""\

            WITH our_transfers AS (
                     SELECT *
                       FROM transfers
                      WHERE "timestamp" >= %(ts_start)s
                        AND status = 'succeeded'
                 )
               , our_tips AS (
                     SELECT *
                       FROM our_transfers
                      WHERE context = 'tip'
                 )
               , our_takes AS (
                     SELECT *
                       FROM our_transfers
                      WHERE context = 'take'
                 )
            UPDATE paydays
               SET nactive = (
                       SELECT DISTINCT count(*) FROM (
                           SELECT tipper FROM our_transfers
                               UNION
                           SELECT tippee FROM our_transfers
                       ) AS foo
                   )
                 , ntippers = (SELECT count(DISTINCT tipper) FROM our_transfers)
                 , ntippees = (SELECT count(DISTINCT tippee) FROM our_transfers)
                 , ntips = (SELECT count(*) FROM our_tips)
                 , ntakes = (SELECT count(*) FROM our_takes)
                 , take_volume = (SELECT COALESCE(sum(amount), 0) FROM our_takes)
                 , ntransfers = (SELECT count(*) FROM our_transfers)
                 , transfer_volume = (SELECT COALESCE(sum(amount), 0) FROM our_transfers)
             WHERE ts_end='1970-01-01T00:00:00+00'::timestamptz

        """, {'ts_start': self.ts_start})
        log("Updated payday stats.")

    def update_cached_amounts(self):
        with self.db.get_cursor() as cursor:
            cursor.execute(FAKE_PAYDAY)
        log("Updated receiving amounts.")

    def end(self):
        self.ts_end = self.db.one("""
            UPDATE paydays
               SET ts_end=now()
             WHERE ts_end='1970-01-01T00:00:00+00'::timestamptz
         RETURNING ts_end AT TIME ZONE 'UTC'
        """, default=NoPayday).replace(tzinfo=aspen.utils.utc)

    def notify_participants(self):
        pass  # TODO