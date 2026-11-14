-- GIN expression index over each record's discovery sources, a cloudmap
-- `typedURLs` map keyed by URL. The default jsonb_ops opclass serves the
-- key-existence operator `?`, which the existence test on
-- `/metadata/discovery/sources/<url>` uses (see `JsonQuery::indexed_key`);
-- `idx_record_json_gin`'s jsonb_path_ops can't serve an existence test.
-- The query has to spell the path as this constant for the planner to match.
CREATE INDEX idx_record_sources_gin ON record USING GIN ((json #> '{metadata,discovery,sources}'));
