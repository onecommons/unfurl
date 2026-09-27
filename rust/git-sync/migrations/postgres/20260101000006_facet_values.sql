-- The facet values of one extracted path, per the extraction rule in
-- db::record::facet_lateral_pg: an array's elements, an object's keys, or a
-- scalar itself; a missing path (SQL NULL) yields nothing.
--
-- A function so the planner can be told how many rows to expect: it guesses
-- 100 for each built-in set-returning function, and a facet query crosses one
-- expansion per column, so three columns over a few records were estimated in
-- the tens of millions -- enough to trigger JIT compilation, which then took
-- far longer than the query. STRICT keeps it from being inlined, which would
-- put that guess back.
CREATE FUNCTION facet_values(v jsonb) RETURNS SETOF jsonb
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE ROWS 2 AS $$
    SELECT e FROM jsonb_array_elements(
        CASE WHEN jsonb_typeof(v) = 'array' THEN v ELSE '[]'::jsonb END) e
    UNION ALL
    SELECT to_jsonb(k) FROM jsonb_object_keys(
        CASE WHEN jsonb_typeof(v) = 'object' THEN v ELSE '{}'::jsonb END) k
    UNION ALL
    SELECT v WHERE jsonb_typeof(v) NOT IN ('array', 'object')
$$;
