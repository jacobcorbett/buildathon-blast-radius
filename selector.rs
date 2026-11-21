mod patch;
mod test_manifest;

use std::collections::HashMap;
use std::fs;
use std::io::{self, BufWriter, Write};
use std::path::{Path, PathBuf};

use patch::{Edit, FilePatch};
use test_manifest::Test;

#[derive(Default)]
struct Bits(Vec<u64>);

impl Bits {
    fn parse(hex: &str) -> Self {
        let mut words = Vec::new();
        let mut end = hex.len();
        while end > 0 {
            let start = end.saturating_sub(16);
            words.push(u64::from_str_radix(&hex[start..end], 16).unwrap());
            end = start;
        }
        Self(words)
    }

    fn union(&mut self, other: &Self) {
        if self.0.len() < other.0.len() {
            self.0.resize(other.0.len(), 0);
        }
        for (word, other) in self.0.iter_mut().zip(&other.0) {
            *word |= other;
        }
    }

    fn contains(&self, index: usize) -> bool {
        self.0
            .get(index / 64)
            .is_some_and(|word| word & (1 << (index % 64)) != 0)
    }
}

struct Scope {
    first: usize,
    last: usize,
    indent: usize,
    tests: Bits,
}

struct TokenLocation {
    line: usize,
    start: usize,
    end: usize,
}

impl TokenLocation {
    fn read(fields: &mut std::str::Split<'_, char>) -> Self {
        Self {
            line: fields.next().unwrap().parse().unwrap(),
            start: fields.next().unwrap().parse().unwrap(),
            end: fields.next().unwrap().parse().unwrap(),
        }
    }

    fn replacement<'a>(&self, edit: &Edit<'a>) -> Option<(&'a str, &'a str)> {
        let ([old], [new]) = (edit.removed.as_slice(), edit.added.as_slice()) else {
            return None;
        };
        if edit.before + 1 != self.line {
            return None;
        }
        let (prefix, rest) = old.split_at_checked(self.start)?;
        let (literal, suffix) = rest.split_at_checked(self.end - self.start)?;
        Some((literal, new.strip_prefix(prefix)?.strip_suffix(suffix)?))
    }

    fn matches(&self, edit: &Edit<'_>, replacement: fn(&str) -> Option<&'static str>) -> bool {
        self.replacement(edit)
            .is_some_and(|(old, new)| replacement(old) == Some(new))
    }

    fn numeric_or_boolean(&self, edit: &Edit<'_>) -> bool {
        fn decimal(text: &str) -> bool {
            !text.is_empty()
                && text.len() <= 12
                && (text.len() == 1 || !text.starts_with('0'))
                && text.bytes().all(|byte| byte.is_ascii_digit())
        }
        self.replacement(edit)
            .is_some_and(|(old, new)| flip_bool(old) == Some(new) || (decimal(old) && decimal(new)))
    }
}

struct ArgumentFlag {
    location: TokenLocation,
    tests: Bits,
}

fn flip_bool(literal: &str) -> Option<&'static str> {
    match literal {
        "True" => Some("False"),
        "False" => Some("True"),
        _ => None,
    }
}

fn equality_operator(operator: &str) -> Option<&'static str> {
    let mut words = operator.split_whitespace();
    match (words.next()?, words.next(), words.next()) {
        ("is", None, None) => Some("=="),
        ("is", Some("not"), None) => Some("!="),
        _ => None,
    }
}

struct FileProfile {
    conditions: HashMap<usize, Bits>,
    usage: Bits,
    scopes: Vec<Scope>,
    readers: Bits,
    child_usage: Bits,
    argument_flags: Vec<ArgumentFlag>,
    literals: Vec<ArgumentFlag>,
    identity_comparisons: Vec<TokenLocation>,
}

#[derive(Default)]
struct Profile<'a> {
    introspection: Bits,
    ids: HashMap<&'a str, usize>,
    always: Bits,
    files: HashMap<&'a str, FileProfile>,
}

impl<'a> Profile<'a> {
    fn load(text: &'a str) -> Self {
        let mut profile = Self::default();
        let mut current = "";
        for line in text.lines() {
            let mut fields = line.split('\t');
            match fields.next().unwrap() {
                "T" => {
                    profile
                        .ids
                        .insert(fields.next().unwrap(), profile.ids.len());
                }
                "I" => profile.introspection = Bits::parse(fields.next().unwrap()),
                "U" => profile.always = Bits::parse(fields.next().unwrap()),
                "F" => {
                    current = fields.next().unwrap();
                    let usage = Bits::parse(fields.next().unwrap());
                    profile.files.insert(
                        current,
                        FileProfile {
                            conditions: HashMap::new(),
                            usage,
                            scopes: Vec::new(),
                            readers: Bits::default(),
                            child_usage: Bits::default(),
                            argument_flags: Vec::new(),
                            literals: Vec::new(),
                            identity_comparisons: Vec::new(),
                        },
                    );
                }
                "B" => {
                    let line = fields.next().unwrap().parse().unwrap();
                    let hits = Bits::parse(fields.next().unwrap());
                    profile.files.get_mut(current).unwrap().conditions.insert(line, hits);
                }
                "S" => {
                    let first = fields.next().unwrap().parse().unwrap();
                    let last = fields.next().unwrap().parse().unwrap();
                    let indent = fields.next().unwrap().parse().unwrap();
                    let tests = Bits::parse(fields.next().unwrap());
                    profile.files.get_mut(current).unwrap().scopes.push(Scope {
                        first,
                        last,
                        indent,
                        tests,
                    });
                }
                "R" => {
                    profile.files.get_mut(current).unwrap().readers =
                        Bits::parse(fields.next().unwrap());
                }
                "C" => {
                    profile.files.get_mut(current).unwrap().child_usage =
                        Bits::parse(fields.next().unwrap());
                }
                kind @ ("A" | "N") => {
                    let location = TokenLocation::read(&mut fields);
                    let tests = Bits::parse(fields.next().unwrap());
                    let file = profile.files.get_mut(current).unwrap();
                    let records = if kind == "A" {
                        &mut file.argument_flags
                    } else {
                        &mut file.literals
                    };
                    records.push(ArgumentFlag { location, tests });
                }
                "Q" => {
                    let location = TokenLocation::read(&mut fields);
                    let semantics = fields.next().unwrap();
                    if !semantics.is_empty()
                        && semantics
                            .split(',')
                            .all(|kind| matches!(kind, "identity" | "string_enum"))
                    {
                        profile
                            .files
                            .get_mut(current)
                            .unwrap()
                            .identity_comparisons
                            .push(location);
                    }
                }
                _ => panic!("unexpected build-time profile record"),
            }
        }
        profile
    }

    // Unknown syntax, paths and broad shared changes select the whole suite.
    fn select(&self, text: &str, source: &Path) -> Option<Bits> {
        let mut selected = Bits::default();
        let files = patch::parse(text)?;
        let single_edit = files.len() == 1 && files[0].edits.len() == 1;
        for file in files {
            let path = file.path;
            let Some(profile) = self.files.get(path) else {
                if matches!(path, "README.md" | "CHANGELOG.md") {
                    continue;
                }
                return None;
            };
            if (path.starts_with("sqlglot/") || path.starts_with("tests/")) && path.ends_with(".py")
            {
                let original = fs::read_to_string(source.join(path)).ok()?;
                let updated = file.apply(&original)?;
                let comment_edit = file.edits.iter().all(comments_only);
                selected.union(&profile.readers);
                match checked_syntax(&original, &updated, comment_edit) {
                    0 => return None,
                    2 => {
                        selected.union(&profile.child_usage);
                        if path.starts_with("tests/") {
                            selected.union(&profile.usage);
                        }
                    }
                    // Other edits could change the operands and invalidate the observation.
                    _ if single_edit
                        && profile
                            .identity_comparisons
                            .iter()
                            .any(|site| site.matches(&file.edits[0], equality_operator)) =>
                    {
                        selected.union(&profile.child_usage);
                    }
                    _ => {
                        if self.select_python(&file, profile, &mut selected).is_none() {
                            if !path.starts_with("sqlglot/") || !equivalent_python(&original, &updated) {
                                return None;
                            }
                            selected.union(&self.introspection);
                            selected.union(&profile.child_usage);
                        }
                    }
                }
            } else {
                selected.union(&profile.usage);
            }
            selected.union(&self.always);
        }
        Some(selected)
    }

    fn select_python(
        &self,
        file: &FilePatch<'_>,
        profile: &FileProfile,
        selected: &mut Bits,
    ) -> Option<()> {
        for edit in &file.edits {
            if edit.removed.iter().chain(&edit.added).any(|line| {
                line.split(|character: char| !character.is_alphanumeric() && character != '_')
                    .any(|word| matches!(word, "yield" | "global" | "nonlocal"))
            }) {
                return None;
            }
            if let Some(hits) = profile.conditions.get(&(edit.before + 1))
                .filter(|_| changed_condition(edit))
            {
                selected.union(hits);
                selected.union(&profile.child_usage);
            } else if let Some(literal) = profile
                .literals
                .iter()
                .find(|literal| literal.location.numeric_or_boolean(edit))
            {
                selected.union(&literal.tests);
                selected.union(&profile.child_usage);
            } else if let Some(scope) = profile.scopes.iter().find(|scope| inside(scope, edit)) {
                selected.union(&scope.tests);
            } else if let Some(flag) = profile
                .argument_flags
                .iter()
                .find(|flag| flag.location.matches(edit, flip_bool))
            {
                selected.union(&flag.tests);
                selected.union(&profile.child_usage);
            } else if component_file(file.path) || file.path.starts_with("tests/") {
                selected.union(&profile.usage);
            } else {
                return None;
            }
        }
        Some(())
    }
}

unsafe extern "C" {
    fn python_syntax(
        old: *const u8,
        old_len: u32,
        updated: *const u8,
        new_len: u32,
        compare_comments: bool,
    ) -> std::ffi::c_int;
}

fn checked_syntax(old: &str, updated: &str, compare_comments: bool) -> i32 {
    const MAX_SOURCE: usize = 1024 * 1024;
    if old.len().max(updated.len()) > MAX_SOURCE {
        return 0;
    }
    // The bridge borrows both bounded UTF-8 buffers only for this call.
    unsafe {
        python_syntax(
            old.as_ptr(),
            old.len() as u32,
            updated.as_ptr(),
            updated.len() as u32,
            compare_comments,
        )
    }
}

fn equivalent_python(old: &str, updated: &str) -> bool {
    use std::process::{Command, Stdio};
    let Ok(mut child) = Command::new("python").args(["-IS", "/app/semantic_diff.py"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).spawn() else { return false };
    let mut input = child.stdin.take().unwrap();
    let sent = input.write_all(old.as_bytes()).and_then(|()| input.write_all(&[0]))
        .and_then(|()| input.write_all(updated.as_bytes()));
    drop(input);
    child.wait_with_output().is_ok_and(|out| sent.is_ok() && out.status.success()
        && matches!(out.stdout.as_slice(), b"code\n" | b"docs\n"))
}

fn component_file(path: &str) -> bool {
    let parts: Vec<_> = path.split('/').collect();
    matches!(parts.as_slice(),
        ["sqlglot", "dialects" | "parsers" | "generators" | "typing", name]
        if !matches!(*name, "__init__.py" | "dialect.py" | "base.py"))
}

fn inside(scope: &Scope, edit: &Edit<'_>) -> bool {
    let first = edit.before + 1;
    let last = edit.before + edit.removed.len();
    // An insertion must have both old neighbours in the body.
    if first < scope.first
        || last > scope.last
        || (edit.removed.is_empty()
            && (first > scope.last || (first == scope.first && !comments_only(edit))))
    {
        return false;
    }
    edit.added.iter().all(|line| {
        line.trim().is_empty()
            || line.as_bytes().iter().take_while(|&&ch| ch == b' ').count() >= scope.indent
    })
}

fn comments_only(edit: &Edit<'_>) -> bool {
    edit.before >= 2
        && edit.removed.iter().chain(&edit.added).all(|line| {
            let line = line.trim_start();
            line.is_empty()
                || line.strip_prefix('#').is_some_and(|comment| {
                    let comment = comment.trim_start();
                    !comment.starts_with("type:") && !comment.starts_with("coding")
                })
        })
}

fn changed_condition(edit: &Edit<'_>) -> bool {
    let ([old], [new]) = (edit.removed.as_slice(), edit.added.as_slice()) else {
        return false;
    };
    let indent = old.len() - old.trim_start_matches(' ').len();
    let Some(new) = new.strip_prefix(&old[..indent]) else { return false };
    ["if ", "elif ", "while "].iter().any(|keyword| {
        let variable_names = |line: &str| line.split_whitespace()
            .filter(|word| !matches!(*word, "and" | "or" | "not")).collect::<Vec<_>>().join(" ");
        variable_names(&old[indent..]) == variable_names(new)
        && [&old[indent..], new].iter().all(|line| {
            line.strip_prefix(keyword).and_then(|s| s.strip_suffix(':')).is_some_and(|s|
                !s.is_empty() && s.bytes().all(|c| c.is_ascii_alphanumeric() || b" _.".contains(&c))
                && !s.split_whitespace().any(|word| matches!(word, "await" | "lambda")))
        })
    })
}

fn schedule(tests: &[&Test]) -> Vec<Vec<usize>> {
    if tests.is_empty() {
        return Vec::new();
    }
    let total: u64 = tests.iter().map(|test| test.millis).sum();
    let mut best_cost = u64::MAX;
    let mut best = Vec::new();
    for count in 1..=tests.len().min(8) {
        let floor = total.div_ceil(count as u64).max(tests[0].millis);
        if floor + 2000 * count as u64 >= best_cost {
            continue;
        }
        let mut loads = vec![0; count];
        let mut workers = vec![Vec::new(); count];
        for test in tests {
            let lightest = (0..count)
                .min_by_key(|&worker| (loads[worker], workers[worker].len()))
                .unwrap();
            loads[lightest] += test.millis;
            workers[lightest].push(test.number);
        }
        let cost = loads.into_iter().max().unwrap() + 2000 * count as u64;
        if cost < best_cost {
            best_cost = cost;
            best = workers;
        }
    }
    best
}

fn emit(out: &mut impl Write, commit: &str, workers: &[Vec<usize>]) -> io::Result<()> {
    write!(
        out,
        "{{\"commit\":{},\"workers\":[",
        test_manifest::quote(commit)
    )?;
    for (index, worker) in workers.iter().enumerate() {
        if index != 0 {
            write!(out, ",")?;
        }
        write!(out, "[")?;
        for (position, number) in worker.iter().enumerate() {
            if position != 0 {
                write!(out, ",")?;
            }
            write!(out, "{number}")?;
        }
        write!(out, "]")?;
    }
    writeln!(out, "]}}")
}

fn main() -> io::Result<()> {
    let data = Path::new("/data");
    let profile_text = fs::read_to_string("/app/profile.tsv")?;
    let profile = Profile::load(&profile_text);
    let source = Path::new("/app/source");
    let mut tests = test_manifest::parse(&fs::read_to_string(data.join("tests.json"))?)
        .expect("invalid scorer test manifest");
    tests.sort_unstable_by_key(|test| (std::cmp::Reverse(test.millis), test.number));
    let indexed: Vec<_> = tests
        .iter()
        .map(|test| profile.ids.get(test.id.as_str()).copied())
        .collect();
    let all = schedule(&tests.iter().collect::<Vec<_>>());
    let mut paths: Vec<PathBuf> = fs::read_dir(data.join("commits"))?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<io::Result<_>>()?;
    paths.retain(|path| {
        path.extension()
            .is_some_and(|extension| extension == "patch")
    });
    paths.sort();
    let mut out = BufWriter::new(io::stdout().lock());
    for path in paths {
        let commit = path
            .file_stem()
            .and_then(|stem| stem.to_str())
            .expect("invalid commit name");
        let patch = fs::read_to_string(&path);
        if let Some(selected) = patch.ok().and_then(|patch| profile.select(&patch, source)) {
            let tests: Vec<_> = tests
                .iter()
                .zip(&indexed)
                .filter_map(|(test, index)| {
                    index
                        .is_none_or(|index| selected.contains(index))
                        .then_some(test)
                })
                .collect();
            emit(&mut out, commit, &schedule(&tests))?;
        } else {
            emit(&mut out, commit, &all)?;
        }
    }
    out.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn condition_edits_preserve_names_and_header_boundaries() {
        let mut edit = Edit { before: 12, removed: vec!["    if x and y:"], added: vec!["    if x or y:"] };
        assert!(changed_condition(&edit));
        for replacement in ["    if z and y:", "    while x and y:", "if x and y:",
                            "    if x or y: return", "    if x or f(y):"] {
            edit.added = vec![replacement];
            assert!(!changed_condition(&edit));
        }
    }

    #[test]
    fn worker_overhead_is_included_in_the_schedule() {
        let tests: Vec<_> = [9000, 7000, 6000, 4000, 1000]
            .into_iter()
            .enumerate()
            .map(|(number, millis)| Test {
                id: String::new(),
                number,
                millis,
            })
            .collect();
        let workers = schedule(&tests.iter().collect::<Vec<_>>());
        let mut numbers: Vec<_> = workers.iter().flatten().copied().collect();
        numbers.sort_unstable();
        assert_eq!(numbers, (0..tests.len()).collect::<Vec<_>>());
        assert_eq!(workers.len(), 3);
        assert!(schedule(&[]).is_empty());
        assert_eq!(schedule(&[&tests[0], &tests[4]]).len(), 1);
    }

    #[test]
    fn checks_the_operator_and_all_surrounding_bytes() {
        let site = TokenLocation {
            line: 3,
            start: 5,
            end: 11,
        };
        let mut edit = Edit {
            before: 2,
            removed: vec!["left is not right"],
            added: vec!["left != right"],
        };
        assert!(site.matches(&edit, equality_operator));
        edit.added = vec!["left != other"];
        assert!(!site.matches(&edit, equality_operator));
    }

    #[test]
    fn only_real_comments_can_leave_python_tokens_unchanged() {
        assert_eq!(checked_syntax("x = 1\n", "# note\nx = 1\n", true), 2);
        let original = "x = \"\"\"first\nlast\"\"\"\n";
        assert_eq!(
            checked_syntax(original, "x = \"\"\"first\n# note\nlast\"\"\"\n", true),
            1
        );
        assert_eq!(checked_syntax("x = 1\n", "x = (1\n", false), 0);
    }
}
