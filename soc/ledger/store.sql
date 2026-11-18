CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY CHECK (seq > 0),
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    time TEXT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (json_valid(actor)),
    commitment_id TEXT,
    base_version INTEGER CHECK (base_version >= 0),
    new_version INTEGER,
    ledger_version INTEGER NOT NULL UNIQUE CHECK (ledger_version = seq),
    chunk_id TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    UNIQUE (commitment_id, new_version),
    CHECK (
        (type IN ('Provisional', 'Ratified', 'Revised', 'Superseded', 'Revoked')
            AND commitment_id IS NOT NULL AND base_version IS NOT NULL
            AND new_version IS NOT NULL AND new_version = base_version + 1)
        OR (type NOT IN ('Provisional', 'Ratified', 'Revised', 'Superseded', 'Revoked') AND new_version IS NULL)
    )
) STRICT;

CREATE INDEX IF NOT EXISTS chunk_build_order
ON events (chunk_id, seq DESC) WHERE type = 'ChunkBuilt';

CREATE TABLE IF NOT EXISTS public_episode (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT NOT NULL,
    sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS operation_authority (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    operation TEXT NOT NULL,
    principal TEXT NOT NULL
) STRICT;

CREATE TRIGGER IF NOT EXISTS event_no_update
BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS event_no_delete
BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS event_insert_order
BEFORE INSERT ON events BEGIN
    SELECT RAISE(ABORT, 'event sequence must advance by one')
    WHERE NEW.seq <> COALESCE((SELECT MAX(seq) FROM events), 0) + 1;

    SELECT RAISE(ABORT, 'event identity already exists')
    WHERE EXISTS (SELECT 1 FROM events WHERE event_id = NEW.event_id);

    SELECT RAISE(ABORT, 'run identity cannot change')
    WHERE NEW.run_id <> (SELECT run_id FROM events WHERE seq = 1);

    SELECT RAISE(ABORT, 'commitment version must advance by one')
    WHERE NEW.new_version IS NOT NULL
        AND NEW.new_version <> COALESCE((
            SELECT MAX(new_version) FROM events WHERE commitment_id = NEW.commitment_id
        ), 0) + 1;
END;
