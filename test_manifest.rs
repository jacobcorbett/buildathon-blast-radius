// The scorer supplies trusted JSON. Read its test manifest without runtime dependencies.
use std::collections::HashMap;

#[derive(Debug)]
enum Json {
    Object(HashMap<String, Json>),
    Array(Vec<Json>),
    String(String),
    Number(f64),
    Other,
}

struct Parser<'a>(&'a str);

impl Parser<'_> {
    fn take(&mut self, expected: char) -> bool {
        self.0 = self.0.trim_start();
        if let Some(rest) = self.0.strip_prefix(expected) {
            self.0 = rest;
            true
        } else {
            false
        }
    }

    fn string(&mut self) -> Option<String> {
        if !self.take('"') {
            return None;
        }
        let mut value = String::new();
        while let Some(ch) = self.0.chars().next() {
            self.0 = &self.0[ch.len_utf8()..];
            match ch {
                '"' => return Some(value),
                '\\' => {
                    let escape = self.0.chars().next()?;
                    self.0 = &self.0[escape.len_utf8()..];
                    value.push(match escape {
                        '"' | '\\' | '/' => escape,
                        'b' => '\u{0008}',
                        'f' => '\u{000c}',
                        'n' => '\n',
                        'r' => '\r',
                        't' => '\t',
                        'u' => {
                            let high = self.hex_quad()?;
                            let codepoint = if (0xd800..=0xdbff).contains(&high) {
                                self.0 = self.0.strip_prefix("\\u")?;
                                let low = self.hex_quad()?;
                                if !(0xdc00..=0xdfff).contains(&low) {
                                    return None;
                                }
                                0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00)
                            } else {
                                high
                            };
                            char::from_u32(codepoint)?
                        }
                        _ => return None,
                    });
                }
                c if c.is_control() => return None,
                c => value.push(c),
            }
        }
        None
    }

    fn hex_quad(&mut self) -> Option<u32> {
        let value = u32::from_str_radix(self.0.get(..4)?, 16).ok()?;
        self.0 = &self.0[4..];
        Some(value)
    }

    fn value(&mut self) -> Option<Json> {
        if self.take('{') {
            let mut fields = HashMap::new();
            if !self.take('}') {
                loop {
                    let key = self.string()?;
                    if !self.take(':') {
                        return None;
                    }
                    fields.insert(key, self.value()?);
                    if self.take('}') {
                        break;
                    }
                    if !self.take(',') {
                        return None;
                    }
                }
            }
            return Some(Json::Object(fields));
        }
        if self.take('[') {
            let mut items = Vec::new();
            if !self.take(']') {
                loop {
                    items.push(self.value()?);
                    if self.take(']') {
                        break;
                    }
                    if !self.take(',') {
                        return None;
                    }
                }
            }
            return Some(Json::Array(items));
        }
        if self.0.starts_with('"') {
            return Some(Json::String(self.string()?));
        }
        let end = self
            .0
            .find(|ch: char| ch.is_whitespace() || ",]}".contains(ch))
            .unwrap_or(self.0.len());
        let (atom, rest) = self.0.split_at(end);
        self.0 = rest;
        match atom {
            "true" | "false" | "null" => Some(Json::Other),
            _ => atom.parse().ok().map(Json::Number),
        }
    }
}

#[derive(Debug)]
pub struct Test {
    pub id: String,
    pub number: usize,
    pub millis: u64,
}

pub fn parse(text: &str) -> Option<Vec<Test>> {
    let mut parser = Parser(text);
    let Json::Object(mut root) = parser.value()? else {
        return None;
    };
    let Json::Array(tests) = root.remove("tests")? else {
        return None;
    };
    if !parser.0.trim().is_empty() {
        return None;
    }
    tests
        .into_iter()
        .map(|test| {
            let Json::Object(mut fields) = test else {
                return None;
            };
            let Json::String(id) = fields.remove("id")? else {
                return None;
            };
            let Json::Number(number) = fields.remove("number")? else {
                return None;
            };
            let Json::Number(seconds) = fields.remove("seconds")? else {
                return None;
            };
            Some(Test {
                id,
                number: number as usize,
                millis: (seconds * 1000.0).round() as u64,
            })
        })
        .collect()
}

pub fn quote(text: &str) -> String {
    use std::fmt::Write;
    let mut quoted = String::from("\"");
    for ch in text.chars() {
        match ch {
            '"' => quoted.push_str("\\\""),
            '\\' => quoted.push_str("\\\\"),
            c if c <= '\u{1f}' => write!(quoted, "\\u{:04x}", c as u32).unwrap(),
            c => quoted.push(c),
        }
    }
    quoted.push('"');
    quoted
}
