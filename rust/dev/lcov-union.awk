# Merge lcov reports into one record per source file, each line's and
# function's hit count the most any report gives it.
#
# cargo crap combines a file's records only when their function symbols
# match. Reports from builds with different features (the tox job builds
# tosca-solver with `python`, the cargo tests without) have different
# symbols, so it counts every line once per report: a line one report
# covers and another doesn't counts once covered and once not.
#
#   awk -f rust/dev/lcov-union.awk a.info b.info > merged.info

function max(a, b) { return a > b ? a : b }

/^SF:/ {
    file = substr($0, 4)
    if (!(file in seen)) {
        seen[file] = 1
        files[++nfiles] = file
    }
    next
}

/^FN:/ {
    s = substr($0, 4)
    comma = index(s, ",")
    key = file SUBSEP substr(s, comma + 1)
    if (!(key in fn_line)) {
        fn_line[key] = substr(s, 1, comma - 1)
        fns[file] = fns[file] substr(s, comma + 1) "\n"
    }
    next
}

/^FNDA:/ {
    s = substr($0, 6)
    comma = index(s, ",")
    key = file SUBSEP substr(s, comma + 1)
    fn_hits[key] = max(fn_hits[key] + 0, substr(s, 1, comma - 1) + 0)
    next
}

/^DA:/ {
    s = substr($0, 4)
    comma = index(s, ",")
    line = substr(s, 1, comma - 1) + 0
    key = file SUBSEP line
    if (!(key in da)) {
        lines[file] = lines[file] line "\n"
        da[key] = 0
    }
    da[key] = max(da[key], substr(s, comma + 1) + 0)
    next
}

END {
    for (i = 1; i <= nfiles; i++) {
        file = files[i]
        print "SF:" file
        n = split(fns[file], names, "\n")
        fnf = 0
        fnh = 0
        for (j = 1; j < n; j++) {
            print "FN:" fn_line[file SUBSEP names[j]] "," names[j]
        }
        for (j = 1; j < n; j++) {
            hits = fn_hits[file SUBSEP names[j]] + 0
            print "FNDA:" hits "," names[j]
            fnf++
            if (hits > 0) fnh++
        }
        print "FNF:" fnf
        print "FNH:" fnh
        n = split(lines[file], nums, "\n")
        lf = 0
        lh = 0
        for (j = 1; j < n; j++) {
            hits = da[file SUBSEP nums[j]]
            print "DA:" nums[j] "," hits
            lf++
            if (hits > 0) lh++
        }
        print "LF:" lf
        print "LH:" lh
        print "end_of_record"
    }
}
