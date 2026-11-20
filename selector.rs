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

struct FileProfile {
    usage: Bits,
    scopes: Vec<Scope>,
    readers: Bits,
}

#[derive(Default)]
struct Profile<'a> {
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
                "U" => profile.always = Bits::parse(fields.next().unwrap()),
                "F" => {
                    current = fields.next().unwrap();
                    let usage = Bits::parse(fields.next().unwrap());
                    profile.files.insert(
                        current,
                        FileProfile {
                            usage,
                            scopes: Vec::new(),
                            readers: Bits::default(),
                        },
                    );
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
                "L" => (),
                _ => panic!("unexpected build-time profile record"),
            }
        }
        profile
    }

    // Unknown syntax, paths and broad shared changes select the whole suite.
    fn select(&self, text: &str, source: &Path) -> Option<Bits> {
        let mut selected = Bits::default();
        let files = patch::parse(text)?;
        for file in files {
            let path = file.path;
            let Some(profile) = self.files.get(path) else {
                if matches!(path, "README.md" | "CHANGELOG.md") {
                    continue;
                }
                return None;
            };
            if let Some(source_path) = path.strip_prefix("sqlglot/").filter(|p| p.ends_with(".py"))
            {
                let original = fs::read_to_string(source.join(source_path)).ok()?;
                let updated = file.apply(&original)?;
                let comment_edit = file.edits.iter().all(comments_only);
                selected.union(&profile.readers);
                match checked_syntax(&original, &updated, comment_edit) {
                    0 => return None,
                    2 => (),
                    _ => self.select_python(&file, profile, &mut selected)?,
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
            if let Some(scope) = profile.scopes.iter().find(|scope| inside(scope, edit)) {
                selected.union(&scope.tests);
            } else if component_file(file.path) {
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
    let source = Path::new("/app/source/sqlglot");
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
