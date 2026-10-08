-- Bumped by everything that changes what a render of the file decides --
-- a write or removal, a scan taking the file in, a deletion, a conflict's
-- resolution -- so a write can tell whether another writer overtook its
-- render. A record write doesn't bump it: the record stays pending until
-- a later write puts it on disk.
ALTER TABLE file ADD COLUMN write_seq BIGINT NOT NULL DEFAULT 0;
