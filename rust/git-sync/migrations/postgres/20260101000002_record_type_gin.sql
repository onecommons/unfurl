-- GIN expression index over each record's declared type names.
-- `json -> 'type'` is a cloudmap `typeRef` map keyed by type name;
-- the default jsonb_ops opclass serves the key-existence operators
-- (`?`, `?|`) used by the `type_names` filter in db::record::find.
CREATE INDEX idx_record_type_gin ON record USING GIN ((json -> 'type'));
