// Keep the two hunk coordinates and validate all old lines before using coverage.
#[derive(Debug)]
pub struct Edit<'a> {
    pub before: usize,
    pub removed: Vec<&'a str>,
    pub added: Vec<&'a str>,
}

#[derive(Debug)]
pub struct FilePatch<'a> {
    pub path: &'a str,
    pub edits: Vec<Edit<'a>>,
    context: Vec<(usize, &'a str)>,
}

impl FilePatch<'_> {
    pub fn apply(&self, source: &str) -> Option<String> {
        use std::fmt::Write;

        let lines: Vec<_> = source.lines().collect();
        if self
            .context
            .iter()
            .any(|&(line, text)| lines.get(line - 1) != Some(&text))
        {
            return None;
        }

        let mut updated = String::with_capacity(source.len());
        let mut consumed = 0;
        for edit in &self.edits {
            for line in lines.get(consumed..edit.before)?.iter().chain(&edit.added) {
                writeln!(updated, "{line}").unwrap();
            }
            consumed = edit.before.checked_add(edit.removed.len())?;
        }
        for line in lines.get(consumed..)? {
            writeln!(updated, "{line}").unwrap();
        }
        Some(updated)
    }
}

fn range(text: &str, sign: char) -> Option<(usize, usize)> {
    let text = text.strip_prefix(sign)?;
    let (start, count) = text.split_once(',').unwrap_or((text, "1"));
    let count = count.parse().ok()?;
    let start: usize = start.parse().ok()?;
    let offset = if count == 0 {
        start
    } else {
        start.checked_sub(1)?
    };
    offset.checked_add(count)?;
    Some((offset, count))
}

fn file_path<'a>(line: &'a str, prefix: &str) -> Option<&'a str> {
    let path = line.strip_prefix(prefix)?.split('\t').next()?;
    if path.is_empty() || path.split('/').any(|part| matches!(part, "" | "." | "..")) {
        return None;
    }
    Some(path)
}

pub fn parse(text: &str) -> Option<Vec<FilePatch<'_>>> {
    let mut files = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut current = None;
    let mut lines = text.lines().peekable();
    let mut old_end = 0;
    let mut new_end = 0;
    while let Some(line) = lines.next() {
        if line.starts_with("diff --git ") {
            if let Some(file) = current.take() {
                files.push(file);
            }
            old_end = 0;
            new_end = 0;
            while lines.peek().is_some_and(|line| line.starts_with("index ")) {
                lines.next();
            }
            let old = file_path(lines.next()?, "--- a/")?;
            let new = file_path(lines.next()?, "+++ b/")?;
            if old != new || !seen.insert(old) {
                return None;
            }
            current = Some(FilePatch {
                path: old,
                edits: Vec::new(),
                context: Vec::new(),
            });
        } else if line.starts_with("@@ ") {
            let file = current.as_mut()?;
            let mut header = line.split_whitespace().skip(1);
            let (mut old, mut remaining_old) = range(header.next()?, '-')?;
            let (mut new, mut remaining_new) = range(header.next()?, '+')?;
            if header.next()? != "@@"
                || old < old_end
                || new < new_end
                || old - old_end != new - new_end
            {
                return None;
            }
            let mut edit: Option<Edit> = None;
            while remaining_old != 0 || remaining_new != 0 {
                let (kind, content) = lines.next()?.split_at_checked(1)?;
                match kind {
                    " " | "-" if remaining_old != 0 => {
                        if kind == " " {
                            if remaining_new == 0 {
                                return None;
                            }
                            remaining_new -= 1;
                            new += 1;
                            file.edits.extend(edit.take());
                        } else {
                            edit.get_or_insert_with(|| Edit {
                                before: old,
                                removed: Vec::new(),
                                added: Vec::new(),
                            })
                            .removed
                            .push(content);
                        }
                        remaining_old -= 1;
                        old += 1;
                        file.context.push((old, content));
                    }
                    "+" if remaining_new != 0 => {
                        edit.get_or_insert_with(|| Edit {
                            before: old,
                            removed: Vec::new(),
                            added: Vec::new(),
                        })
                        .added
                        .push(content);
                        remaining_new -= 1;
                        new += 1;
                    }
                    _ => return None,
                }
            }
            file.edits.extend(edit.take());
            old_end = old;
            new_end = new;
        } else {
            return None;
        }
    }
    files.extend(current);
    (!files.is_empty() && files.iter().all(|file| !file.edits.is_empty())).then_some(files)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn applies_separate_hunks_and_checks_old_context() {
        let diff = concat!(
            "diff --git a/example.py b/example.py\n",
            "index 1111111..2222222 100644\n",
            "--- a/example.py\n+++ b/example.py\n",
            "@@ -1,2 +1,3 @@\n first\n+extra\n second\n",
            "@@ -4,2 +5,2 @@\n fourth\n-fifth\n+last\n",
        );
        let files = parse(diff).unwrap();
        let original = "first\nsecond\nthird\nfourth\nfifth\n";
        assert_eq!(
            files[0].apply(original).as_deref(),
            Some("first\nextra\nsecond\nthird\nfourth\nlast\n"),
        );
        assert!(
            files[0]
                .apply(&original.replace("fourth", "other"))
                .is_none()
        );
    }

    #[test]
    fn rejects_non_content_edits_and_truncated_hunks() {
        let headers = "diff --git a/test b/test\n--- a/test\n+++ b/test\n";
        assert!(parse(headers).is_none());
        assert!(parse(&format!("{headers}@@ -1 +1 @@\n-removed\n")).is_none());
        assert!(parse(&format!("{headers}@@ -9 +10 @@\n-removed\n+added\n")).is_none());
        assert!(
            parse(&format!(
                "{headers}@@ -1 +1 @@\n-removed\n+added\n\\ No newline at end of file\n"
            ))
            .is_none()
        );
    }
}
