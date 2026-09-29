-- This database's identity: `record.key_id`s are its row ids, so a
-- commit's rollup names its ids under it (`Git-Sync-Database`) and a
-- reader takes only ids named under its own.
CREATE TABLE database_identity (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    uuid TEXT    NOT NULL
);
INSERT INTO database_identity (id, uuid) VALUES (1, lower(hex(randomblob(16))));
