-- Blob OID of the exact bytes this file's records were parsed from.
--
-- Lets a write tell whether the database's picture of a file is still
-- current. `commit_id` cannot: it names the commit that last touched the
-- path, so it is unchanged by an uncommitted edit and shared by files
-- that differ.
--
-- NULL for a file registered by a record write rather than a scan --
-- nothing has been parsed from it, so there is nothing to compare.
ALTER TABLE file ADD COLUMN source_oid TEXT;
