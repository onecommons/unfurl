-- A counter on each file, incremented whenever the file's state changes
-- in a way that affects what writing it would produce: the file is written
-- or removed, a scan takes it in, it is deleted, or one of its conflicts is
-- resolved. A write reads the counter before rendering and commits only if
-- it is unchanged; otherwise another writer changed the file meanwhile, and
-- it renders again. Writing a record doesn't increment it: the record stays
-- pending until the next write of the file.
ALTER TABLE file ADD COLUMN write_seq INTEGER NOT NULL DEFAULT 0;
