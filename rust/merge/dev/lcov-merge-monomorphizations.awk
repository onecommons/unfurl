# Rewrite an lcov.info so that all monomorphic instantiations of a
# generic function report the same hit count (the max across the
# group). This makes per-symbol coverage tools (cargo crap, etc.)
# stop reporting "phantom" zero entries for instantiations that
# compiled but were never linked into a running test binary.
#
# Usage:
#   awk -f lcov-merge-monomorphizations.awk lcov.info > lcov.clean.info
#
# Then point your tool at lcov.clean.info, e.g.:
#   cargo crap --top 20 --lcov lcov.clean.info
#
# What it does:
# - For each (source_file, function_line), find the maximum FNDA
#   hit count across all instantiations of the function declared at
#   that line.
# - Rewrite every FNDA line for those instantiations to that max.
# - Leave FN/SF/DA/BRDA/etc. untouched, so line-level coverage and
#   structural totals are unchanged.

/^SF:/ { file = substr($0, 4) }

/^FN:/ {
    s = substr($0, 4)
    comma = index(s, ",")
    line = substr(s, 1, comma - 1)
    name = substr(s, comma + 1)
    fn_loc[name] = file ":" line
}

/^FNDA:/ {
    s = substr($0, 6)
    comma = index(s, ",")
    hits = substr(s, 1, comma - 1) + 0
    name = substr(s, comma + 1)
    loc = fn_loc[name]
    if (loc != "" && hits > loc_max[loc]) {
        loc_max[loc] = hits
    }
}

{ all[NR] = $0 }

END {
    for (i = 1; i <= NR; i++) {
        line = all[i]
        if (substr(line, 1, 5) == "FNDA:") {
            s = substr(line, 6)
            comma = index(s, ",")
            name = substr(s, comma + 1)
            loc = fn_loc[name]
            if (loc != "") {
                # Coerce via `+ 0`: when no FNDA for this location
                # had hits > 0, loc_max[loc] was never assigned and
                # would print as empty (producing the invalid
                # `FNDA:,name` syntax). `0 + ""` == 0 in awk.
                print "FNDA:" (loc_max[loc] + 0) "," name
                continue
            }
        }
        print line
    }
}
