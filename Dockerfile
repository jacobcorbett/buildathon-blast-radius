FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS upstream
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /upstream
COPY requirements.lock /build/requirements.lock
RUN python -m pip install --no-cache-dir -r /build/requirements.lock
RUN python -c 'import tarfile, urllib.request; url = "https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa"; tarfile.open(fileobj=urllib.request.urlopen(url, timeout=60), mode="r|gz").extractall(filter="data")' \
    && mv sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa source

FROM upstream AS calibration
COPY calibrate.py /build/
RUN cd source && PYTHONPATH=. python /build/calibrate.py /build/cpu.json
RUN cd source && python -m compileall -q --invalidation-mode checked-hash sqlglot tests

FROM upstream AS profile
COPY profile_tests.py operand_profile.py sitecustomize.py value_profile.py param_flow.py argument_usage.py tokenizer_profile.py pool_observer.py /build/
RUN cd source && ulimit -s 65536 && PYTHONPATH=. python /build/profile_tests.py /build/profile.tsv

FROM calibration AS witnesses
COPY optimizer_state.py capture_optimizer.py pool_observer.py worker_state.py /build/
RUN cd source && ulimit -s 65536 && PYTHONPATH=. python /build/capture_optimizer.py /build/optimizer-calls /build/cpu.json \
    && PYTHONPATH=. python /build/capture_optimizer.py /build/optimizer-calls --validate

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS syntax-deps
WORKDIR /deps
COPY fetch_syntax.py /build/fetch_syntax.py
RUN python /build/fetch_syntax.py

FROM rust:1.99.1-slim@sha256:f6c4a35d21dbb3c89d7a8d056c193e94fc09037f2b58dd858b09c39a47dd02d7 AS compile
WORKDIR /build
COPY --from=syntax-deps /deps /deps
COPY syntax.c ./
RUN tree=/deps/tree-sitter/package/vendor/tree-sitter/lib && grammar=/deps/tree-sitter-python/package/src \
    && cc -O2 -D_DEFAULT_SOURCE -I"$tree/include" -I"$tree/src" -I"$grammar" -c \
        "$tree/src/lib.c" "$grammar/parser.c" "$grammar/scanner.c" syntax.c \
    && ar rcs libsyntax.a lib.o parser.o scanner.o syntax.o
COPY selector.rs patch.rs test_manifest.rs ./
RUN rustc --edition=2024 -D warnings --test -L native=/build -l static=syntax selector.rs -o /build/checks \
    && /build/checks
RUN rustc --edition=2024 -D warnings -C opt-level=3 -C strip=symbols \
    -L native=/build -l static=syntax selector.rs -o /build/selector

FROM calibration
WORKDIR /app
COPY semantic_diff.py verify_tests.py optimizer_state.py optimizer_check.py worker_state.py pool_observer.py /app/
COPY --from=witnesses /build/optimizer-calls /app/optimizer-calls
RUN ln -s /upstream/source /app/source
COPY --from=profile /upstream/source/LICENSE /usr/share/doc/sqlglot/LICENSE
COPY --from=profile /build/profile.tsv /app/profile.tsv
COPY --from=compile /build/selector /app/selector
COPY --from=syntax-deps /deps/tree-sitter/package/LICENSE /usr/share/doc/tree-sitter/LICENSE
COPY --from=syntax-deps /deps/tree-sitter-python/package/LICENSE /usr/share/doc/tree-sitter-python/LICENSE
COPY --from=syntax-deps /deps/tree-sitter/package/vendor/tree-sitter/lib/src/unicode/LICENSE /usr/share/doc/tree-sitter/unicode-LICENSE
ENTRYPOINT ["/app/selector"]
