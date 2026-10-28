# Narrative-Template Answers & Diagnostic Analytics — Design Spec

**Status:** Decisions of 2026-10-23 applied (section F). Session 1 (client, task list C12) is implemented (2026-10-24): client PR #21 (the sandbox layer — `pdc_diag`, VALUES/FINDINGS capture and transport —, the `narrative.py` renderer and the value scrubs of B7.6/B7.7) and PR B (branch `feat/narrative-1b-flows-ui`: the single and multi flows, the record fields, the SSE events, the report path, the `/lab` UI and the docs); its deliberate deviations from this spec are listed under "Session 1 implementation notes" at the end of C12 session 1. Session 2 shipped in PDC_Brain PRs #2–#4; B10.2, B10.3 and B10.5 are synced to the merged brain texts (PITFALLS block, pdc_diag key map, block-isolation routing, pitfall 5 on `latest_is_past_period`). B10.3, B10.4 and B10.5 are synced again (2026-10-25) to brain PR #9 (CHECKS, FINDINGS AND SLOTS; PDC_DIAG ON RETRY; template_patterns 2 and 3; `data_gaps`) and its follow-up brain PR #11, which teaches the client PR #24 data gap (`period_status = data_gap`, the B8.4 / B8.5 key map) and corrects the import sentence to what the client PR #24 `pdc_diag` redefinition guard (B8) actually refuses. Session 3 (two-phase) is decided after sessions 1+2 have been tested on the BRB questions; its design stays in this spec, gated behind the capability `two_phase_v1`.
**Location:** `PDC_Client/docs/DIAGNOSTICS_SPEC.md` (client repo; referenced from the DRAFT section of `PROTOCOL.md` in both repos)
**Audience:** the three implementation sessions (client, brain, two-phase integration), reviewers of the data boundary (Article II), the lead.

**Scope:**
- (a) **Narrative-template answer text.** The brain writes the answer as a template with value slots. The client fills the slots locally after execution, so the brain never sees a number.
- (b) **Diagnostic playbook** for "why / what problems / what drives X / what should we do" questions. It uses a deterministic client-side helper library (`pdc_diag`) and allows at most one second code pass.

**Non-goals (v1):**
- Dashboard tiles and the per-item Refresh (`routes/chat.py run_item_refresh`) do not re-render narratives or captions. A caption keeps the values of the turn that produced it.
- No locale-specific number formatting. The hook exists (`pdc_diag._LOCALES`) with one `default` entry only.
- Auto Analytics (`auto_analytics.py`) and `/v1/auto_analytics_plan` do not send `capabilities`. Their behaviour is unchanged except for the null summarize preview (constraint 3, which applies to every summarize call).
- No narrative for greetings, ANSWER, CLARIFICATION, MISSING_DATA or NO_CODE replies.
- No third code pass, ever. No agentic loop.
- No change to `planner_system`, `planner_human_suffix`, `summarizer_system` or `summarizer_human_suffix` bytes.

---

## 1. Constraints and traceability

| # | Constraint (user, not reopenable) | How it is satisfied | Where |
|---|---|---|---|
| C1 | No data values ever reach the brain. From an executed result the client may send back only booleans, column names and enum codes from a fixed list. | VALUES live only in the sandbox, `exec_transport`, `narrative.render` and the local record. FINDINGS are validated (bool / column name of the input frames / fixed ENUM; key literal in code or in the `pdc_diag` key registry) before reaching `/v1/plan` (`findings`), `/v1/describe` (`data_caveat.findings`) or the history marker. Summarize preview is always null. Title and report get templates, never rendered text. Pinning tests use a sentinel value. | A3, B7, D; `tests/test_narrative_boundary.py` |
| C2 | Numbers reach the user through a template the brain writes and the client fills after execution. | Brain: ```` ```narrative ```` / ```` ```caption:<n> ```` fences → response `narrative` / `captions`. Client: `narrative.render(template, values, findings, sid=)`. | A1, A2, A5 |
| C3 | Template present ⇒ no `/v1/describe` and no `/v1/summarize` for that answer. Fallback to summarize sends `preview: null`, and the brain writes a reading guide without a value. | `run_chat_local` skips the answer-level describe/summarize when a narrative is present (A5/A6). Exception: per the user's own item A.5, a visual whose caption index is missing gets today's per-chart describe for that chart only, with an unchanged prompt (decision F26). Backstop `facts` never carry a constant-value hint any more (decision F1, B7.4), so this describe is value-free. Every `brain_client.summarize` call site passes `preview=None` (D7). The brain adds the READING GUIDE block whenever the preview section is absent (D11). | A5, A6, B10.6 |
| C4 | At most two code passes per question. Phase-1 FINDINGS may set `needs_phase2`. The brain plans phase 2 from FINDINGS only. Never a third pass. | Gated behind `two_phase_v1` (session 3, pending the post-testing decision). Without it, the playbook does every step in ONE pass and no second plan is ever requested. With it, the brain sets `diagnostic: true` only on a phase-1 plan (no `findings` in the request), and the client runs at most one extra `/v1/plan` and ignores `needs_phase2` from phase 2 and in the single flow. | B9 |

---

## 2. Architecture overview

### 2.1 One turn, single flow (`_run_single_from_plan` — one PYTHON block, no `###NEXT_PLOT###`)

```
Browser          routes/chat.py worker        run_chat_local                 Sandbox            Brain
  | POST stream ->|                            |                               |                  |
  |               |-- run_chat_multi_plot ---->| plan(capabilities=[...]) ----------------------->|
  |               |                            |<- {kind:PYTHON, code, narrative, captions{"1"}} --|
  |               |                            |-- safe_execute(code) -------->| exec; VALUES,   |
  |               |                            |<- {result, preview, values, findings} ----------  |
  |               |                            | validate_findings; render(narrative), render(caption 1)
  |               |                            | (NO summarize; NO describe while caption 1 exists)|
  |               |<- {single_response, result:{text=marker+template, narrative_rendered, ...}}   |
  |<- done {answer=narrative_rendered, narrative, caption, image/table} -----------------------  |
```

Without a narrative, the flow is today's:
- a visual gets describe, with `data_caveat.findings` when FINDINGS exist;
- a scalar gets summarize with `preview=None`, and the client appends `value_display` locally.

### 2.2 Multi flow (`run_chat_multi_plot` worklist — `###NEXT_PLOT###` reply, or a lone PLOT_CODE)

```
plan -> raw_text (fences already stripped by the brain) + narrative + captions{"n"}
blocks = _extract_multi_plot_blocks(raw_text) -> block_index n = position (1-based), BEFORE table extraction
for each chart block n: render -> retries(<=3) -> values/findings merged -> caption n rendered (or per-chart describe if caption n missing) -> yield partial{answer=caption, phase:1}
for each table block n: execute -> merged -> caption n rendered -> tables collected
after phase-1 blocks: narrative rendered -> yield {narrative:true, phase:1, text}
done {combined_answer = marker+template (brain-safe), display_answer, tables, narrative view}
```

### 2.3 Two-phase (multi flow only)

```
phase 1 (as 2.2) ──► plan.diagnostic && FINDINGS.needs_phase2 is True && not stopped && turn elapsed < 120 s ?
                     no  -> done
                     yes -> yield partial{tables (phase 1), phase:1}          (tables shown before phase 2)
                            yield progress{phase:2, message_key:"lab.phase2_progress"}
                            plan(question, SAME pre-turn history, capabilities, findings=<validated phase-1 FINDINGS>)
                            phase-2 blocks (<=1 retry each, 90 s budget from the phase-2 plan call)
                            yield partial{..., phase:2} per chart
                            yield {narrative:true, phase:2, text}
                            done
                     any failure -> PHASE2_FAILED reason=..., phase-1 answer stands, streamed phase-2 visuals stay
```

On-screen order: phase-1 narrative → phase-1 visuals → phase-1 tables → phase-2 narrative → phase-2 visuals → phase-2 tables. The two narratives go into slots the frontend creates in advance (A5.6).

---

## A. Narrative template

### A1. Planner output and fences (D2)

#### A1.1 Fence grammar (what the model emits)

- Exactly one ```` ```narrative ```` fence per code reply, plus optional ```` ```caption:<n> ```` fences.
- All of them go AFTER the last code block of the reply, never between blocks.
- `n` is the 1-based index of the code block in the reply as split by `###NEXT_PLOT###`. Every ```` ```python ```` / ```` ```plot_code ```` block counts, table blocks included. `1 ≤ n ≤ 12`.
- A template must not contain three consecutive backticks.

````text
```plot_code
fig = ...
VALUES["total_loans"] = int(total)
```
###NEXT_PLOT###
```python
RESULT = summary
```
```narrative
{{total_loans:count}} loans were granted in {{latest_period:period}}. The bar chart shows the count per month.
```
```caption:1
Monthly count; {{latest_period:period}} is highlighted.
```
````

#### A1.2 Brain extraction — `brain_agent._extract_narrative`

```python
_NARRATIVE_INFO_RE = re.compile(r"^narrative$", re.IGNORECASE)
_CAPTION_INFO_RE   = re.compile(r"^caption:([1-9]|1[0-2])$", re.IGNORECASE)
_NARRATIVE_LIKE_RE = re.compile(r"^(narrative|caption)", re.IGNORECASE)    # malformed variants incl. captions:1, narrative_text
NARRATIVE_TEMPLATE_MAX = 4000
CAPTION_TEMPLATE_MAX = 600
_CAPABILITIES = ("narrative_v1", "diagnostics_v1", "two_phase_v1")

def _extract_narrative(text: str, sid: str = "") -> tuple[str | None, dict[int, str], str, dict]:
    """Returns (narrative, captions, stripped_text, stats). Never raises."""
```

Rules:
1. Iterate `_FENCE_RE` (`brain_agent.py:877`, `r"```([^\n`]*)\n([\s\S]*?)```"`). `info = m.group(1).strip()`; `body = m.group(2).strip()`.
2. If `_NARRATIVE_INFO_RE.match(info)`, the fence is a narrative candidate. The first one wins; later ones count as `dup`.
3. If `_CAPTION_INFO_RE.match(info)`, the fence is caption `int(group(1))`. The first per index wins; later ones count as `dup`.
4. Otherwise, if `_NARRATIVE_LIKE_RE.match(info)` (e.g. `caption:0`, `caption:13`, `caption 2`, `narrative text`), the fence is stripped and counted as `malformed`. It is never kept, so the code parsers can never read it as Python.
5. A narrative body over `NARRATIVE_TEMPLATE_MAX`, or a caption body over `CAPTION_TEMPLATE_MAX`, is dropped (not truncated) and counted as `too_long`. An empty body is dropped and counted as `empty`.
6. Every matched span, accepted or dropped, is removed with the `_extract_sql_map` idiom (`brain_agent.py:986-993`): cut out the pieces, then `"\n\n".join(p.strip() for p in pieces if p.strip())`. If there is no span, the text is returned unchanged (the same object).
7. Stats are `{"fences", "narrative_len", "captions", "dup", "too_long", "empty", "malformed"}`. Logs carry counts and lengths only: `NARRATIVE_EXTRACT caller=<plan|retry> narrative_len= captions= dup= too_long= empty= malformed=`. On an exception it logs `NARRATIVE_PARSE_FAILED: <Type>` and returns `(None, {}, text, stats)`.

#### A1.3 Order of extraction (every plan AND retry reply, capability or not)

`plan_code` (`brain_agent.py:1631-1645`) and `retry_code` (`:1811-1845`) become:

```
text -> _extract_narrative  (ALWAYS, first)
     -> _extract_sql_map    (only when live, unchanged)
     -> _tag_reply_code_language
     -> _extract_code_kind
     -> (retry) multi-block join
```

Why extraction runs first: `_extract_code_kind` (defined at `brain_agent.py:523`; its generic-fence regex is at `:564`) takes the FIRST fence of any info string, and the CLARIFICATION substring test (`:552`) would fire on a template that contains "clarify". Stripping first removes both hazards.

Without `narrative_v1` the fences are still stripped, because a model that writes one anyway must not have it executed or shown raw. No response field is added. Log: `NARRATIVE_FENCE_STRIPPED_NO_CAP n=`.

#### A1.4 Response fields — `_attach_narrative`

```python
def _attach_narrative(sid: str, caller: str, result: dict, narrative, captions: dict,
                      stats: dict, caps: frozenset) -> None
```

- Only when `"narrative_v1" in caps`; otherwise it only logs.
- If `result["kind"] in _CODE_KINDS` (`:645`):
  - `n_blocks = len(_extract_multi_plot_blocks(result["raw_text"])) or 1`;
  - a caption with `n > n_blocks` is dropped and counted as `out_of_range`;
  - `result["narrative"] = narrative` when non-empty;
  - `result["captions"] = {str(n): text}` when non-empty. JSON keys are strings.
- For any other kind, every narrative and caption is dropped and counted as `dropped_kind`.
- Log: `NARRATIVE caller= kind= narrative=<bool> captions=<n> out_of_range= dropped_kind=`.

#### A1.5 Retry semantics (client rules)

| Flow | Retry kind | Retry reply's ```` ```narrative ```` | Retry reply's ```` ```caption:k ```` |
|---|---|---|---|
| single | exec-error retry (`_run_single_body` loop, `run_chat_local.py:2107-2146`) | present ⇒ REPLACES the plan narrative; absent ⇒ the plan narrative is KEPT (the retry prompt asks the model to keep the VALUES keys and to write a new narrative only when their meaning changes) | `caption:1` replaces caption 1; absent ⇒ kept |
| multi | per-block exec-error retry (`run_chat_local.py:1698-1757`) for block n | IGNORED | `caption:1` replaces caption n; absent ⇒ caption n kept |
| multi | multi-axes regen for block n (`:1771-1791`), expanding to k blocks | IGNORED | `caption:1` → the first child (block n); `caption:j` for j ≥ 2 dropped; children 2..k have no caption |
| multi | plotly / matrix regen for block n | IGNORED | `caption:1` replaces caption n; absent ⇒ kept |

The retry prompt (A1.6) tells the model to keep the VALUES and FINDINGS keys of `failed_code`.

#### A1.6 Prompt text locations (D1 — convention-preserving reading)

`planner_system` is sent to every client, old ones included, so it cannot change. "Routing rules in planner_system, gated detail in YAML" is therefore read as follows:

| Text | Location | Gate |
|---|---|---|
| Narrative, VALUES, FINDINGS, captions, `pdc_diag` availability, sanity hooks, history templates | `prompts.json` key `planner_narrative_system`, appended to the system instruction right after `planner_system` (before tenant `prompt_tuning_planner`) | `narrative_v1` |
| Narrative output-format sentence | `prompts.json` key `planner_narrative_suffix`, appended to the user prompt after `planner_human_suffix` and before the live block | `narrative_v1` |
| Retry narrative rule | `prompts.json` key `retry_narrative_system`, appended to the retry system instruction after `_build_retry_system_instruction` and after the live retry block | `narrative_v1` on `/v1/retry` |
| Routing to diagnostic/advisory (task_type definitions, precedence, examples) | classifier prompt section built in code (`_diagnostic_classifier_section()`) | `diagnostics_v1` |
| Playbook, multi-block form, `needs_phase2` rule, advisory framing, template patterns, pitfalls | `skills/core/diagnostics.yaml` | `diagnostics_v1` AND (task_type ∈ {diagnostic, advisory} OR classifier `skills_needed` contains `diagnostics`) |
| Phase-2 instruction + FINDINGS listing | code block `_findings_prompt_block(findings)` in the user prompt | the request carries non-empty `findings` |

The exact texts are in B10.3–B10.5.

### A2. Template language (D3) — client module `narrative.py`

#### A2.1 Grammar

```
template  := { text | slot | section }
section   := ( "{{#" name "}}" | "{{^" name "}}" ) body "{{/" name "}}"    -- close name == open name
body      := { text | slot }                                                -- no section inside a section
slot      := "{{" name [ ":" kind ] "}}"                                     -- no whitespace inside braces
name      := [A-Za-z_][A-Za-z0-9_]{0,39}
kind      := [a-z]{1,10}       -- recognised: count|float|pct|ratio|amount|text|period; other ⇒ default kind
text      := any characters; any "{{…}}" that does not match the token regex below is literal text
```

Token regex (single pass, `re.finditer`):

```python
_TOKEN_RE = re.compile(
    r"\{\{(?:(?P<sec>[#^/])(?P<sname>[A-Za-z_][A-Za-z0-9_]{0,39})"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]{0,39})(?::(?P<kind>[a-z]{1,10}))?)\}\}")
```

`parse()` raises `TemplateError(ValueError)` for:
- an open section inside an open section;
- a close without an open;
- a close whose name differs from the open;
- an open section that is never closed.

#### A2.2 Semantics

- **Slot.** If `values[name]` exists, render `pdc_diag.fmt(values[name], kind_or_None)`. If it is missing, render `"n/a"` and log one `NARRATIVE_SLOT_MISSING name=<name>` line per name per render (the name only). `render` itself keeps the `"n/a"`; the display flows then remove every sentence that holds one (the A5.1 quality gate), so the user never sees it.
- **`{{#flag}}…{{/flag}}`** is shown when any of these holds:
  - (a) `findings.get(flag) is True`;
  - (b) `flag in findings` and the value is not a bool (a column name or code);
  - (c) `flag ∈ pdc_diag.FINDINGS_ENUM` and some value in `findings.values()` equals `flag`.
- **`{{^flag}}…{{/flag}}`** is shown exactly when the `#` condition is false.
- **Section names (`_resolve_section`).** An exact FINDINGS key wins. A `FINDINGS_ENUM` code or a `pdc_diag` registry key is itself. Otherwise a name `<prefix>_<code>` whose `<code>` is an enum code or a registry key counts as that code, but ONLY when `<prefix>_` is a prefix the phase's executed code passes to `pdc_diag` (`prefix\s*=\s*['"]([a-z][a-z0-9]{0,9}_)['"]`, collected per absorbed block and handed to `render` as `prefixes=`): `prefix=` renames VALUES keys only, so a planner that passes `prefix="ovl_"` and writes `{{#ovl_metric_definition_overlap}}` means `metric_definition_overlap` (the BRB run-2 English stock warning was lost to this). A prefix the code never passes (`no_`, `not_`, `has_`) is never resolved, because stripping it would flip the condition; this is the brain's template-validation rule too. `brain_view` (title, report) gets no prefixes, so a prefixed section is false there. Otherwise (`render_with_sections` only) a name that is a VALUES key whose value is a bool (`isinstance(v, bool)`) is that bool: `{{#x}}` is shown when it is True, `{{^x}}` when it is False. A non-bool VALUES key (`1`, `0`, `"true"`, None) never acts as a section and stays unknown. A VALUES-bool section is not a finding: it is never listed among the shown sections, so it covers no finding code (`covered_codes`) and no FINDING sentence (A6) depends on it; the render logs one `NARRATIVE_SECTION_FROM_VALUES n=<count>` info line (the distinct names, count only, never a name; none in the `first_sentence_na_slot` marking pass). `brain_view` has no VALUES, so such a section is false there — the safe direction: no VALUES-derived bit reaches /v1/title or /v1/report. BRB run 2 lost both amount bullets of `{{#has_uzs}}` / `{{#has_other}}` (bools the code put in VALUES, not in FINDINGS) to this, and the answer ended on a colon. Any other name is false and logs `NARRATIVE_SECTION_UNKNOWN name=<name>` once per render (the name is a template identifier, escaped and capped at 40 characters). `brain_view` resolves the same way, except that it has no VALUES.
- **Parse errors** carry a kind (`TemplateError.kind`, `narrative.template_error_kind(template)`): `nested` (a section inside a section), `unclosed` (an open never closed), `mismatched` (a close whose name differs, or a close without an open).
- Unused VALUES are ignored.
- **Default kind** (unknown or absent kind): bool → `text` (`"true"`/`"false"`), int → `count`, float → `float`, str → `text`, None → `"n/a"`.
- **Post-processing**, in this order:
  1. `\r\n` / `\r` → `\n`;
  2. strip C0 controls except `\n`/`\t` (`re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)`);
  3. `re.sub(r"[ \t]{2,}", " ", s)`;
  4. `re.sub(r"\n{3,}", "\n\n", s)`;
  5. `.strip()`;
  6. cap.
- Rendering is single-pass: substituted text is never re-scanned, so a VALUES string containing `{{x}}` stays literal.
- **Markdown escaping of string values (decision).** A `str` value rendered with kind `text`/`period` (or by default) has:
  - `*` → `\*`;
  - `_` → `\_`;
  - a leading `-` (after optional spaces) → `\-`.

  Numbers are never escaped. `dashboard.js renderMarkdown` gains one step: AFTER the HTML escape and the
  bold/bullet formatting, it turns `\*`, `\_`, `\-` back into the literal characters. Bold detection
  must skip escaped asterisks, so the escapes are protected with placeholders before formatting.
  Local PDF/PPTX text goes through `narrative.plain(text)`, which removes these three escapes.
  Tests: `test_text_value_markdown_escaped`, `test_render_markdown_unescapes_literals` (structure test),
  `test_plain_removes_escapes`.
- The same post-processing (control-char strip, caps) applies to `value_display` and to every `fmt` text output.
- Output is markdown text, never HTML. HTML escaping happens at the display funnel: `dashboard.js renderMarkdown` (`static/dashboard.js:2463-2493`) escapes via `textContent` before formatting (Art. XV r4).

#### A2.3 Caps

| Item | Cap | Over cap |
|---|---|---|
| Narrative template (received) | 4000 chars | dropped (no template), `NARRATIVE_TEMPLATE_REJECTED reason=too_long` |
| Caption template (received) | 600 chars | that caption dropped, `CAPTION_REJECTED index=<n> reason=too_long` |
| Rendered narrative | 8000 chars | cut at the last whitespace at or before 7999, plus `"…"` |
| Rendered caption | 1200 chars | cut at the last whitespace at or before 1199, plus `"…"` |

#### A2.4 API (`narrative.py`, main app only; imports stdlib, `pdc_diag`, `logger_utils`)

```python
KINDS = ("count", "float", "pct", "ratio", "amount", "text", "period")
TEMPLATE_MAX = 4000; CAPTION_MAX = 600; RENDERED_MAX = 8000; CAPTION_RENDERED_MAX = 1200
FINDINGS_MAX_KEYS = 24; FINDINGS_STR_MAX = 64; MARKER_MAX = 400
class TemplateError(ValueError): ...

def parse(template: str) -> list                          # raises TemplateError
def render(template: str, values: dict, findings: dict, *, sid: str,
           max_len: int = RENDERED_MAX) -> str             # never raises
def fallback_text(template: str, *, max_len: int = RENDERED_MAX) -> str   # never raises
def brain_view(template: str, findings: dict) -> str       # never raises
def clean_template(value, *, max_len: int, sid: str, what: str) -> str | None
def clean_captions(value, *, sid: str) -> dict[int, str]   # {"1": str} -> {1: str}; bad keys/values dropped
def validate_findings(raw, code: str, input_columns: frozenset, sid: str) -> dict
def merge_values(acc: dict, new: dict, *, sid: str) -> None      # later wins; VALUES_KEY_COLLISION n=
def merge_findings(acc: dict, new: dict, *, sid: str) -> None    # later wins; FINDINGS_KEY_COLLISION n=; re-cap 24
def findings_marker(findings: dict) -> str                 # "" when empty
def history_content(template: str, findings: dict) -> str  # marker + "\n" + template, or template
def value_display(safe_preview, lang: str) -> str | None
def answer_lang_code(context_decision: dict | None, question: str) -> str   # "en"|"ka"|"ru"|"uz"
```

- **`render`** calls `parse`. On `TemplateError` or any other exception it logs `NARRATIVE_RENDER_FAILED error=<Type>` and returns `fallback_text(template)`.
- **`fallback_text`** steps (any exception ⇒ `""`):
  1. remove `\{\{([#^])(NAME)\}\}[\s\S]*?\{\{/\2\}\}` blocks;
  2. remove any remaining `\{\{[#^/]NAME\}\}` tokens;
  3. replace every slot token with `"n/a"`;
  4. apply the same post-processing as `render`.
- **`brain_view`** builds the text for `/v1/title` and `/v1/report`:
  - sections are resolved with `findings`, using the same semantics as `render`;
  - each slot becomes `"[" + name.replace("_", " ") + "]"`;
  - a parse error ⇒ `fallback_text` with `"[value]"` instead of `"n/a"`.
  - The result contains no value. Its only finding-derived content is the choice of sections, which carries no more than the marker already does.
- **`answer_lang_code`**: `context_decision["answer_language"]` mapped through `{"English": "en", "Georgian": "ka", "Russian": "ru", "Uzbek": "uz"}`; otherwise `run_chat_local._detect_answer_language(question)`. That name is an import alias at `run_chat_local.py:47` for `schema_builder._detect_language` (`schema_builder.py:352`), which returns only en/ka/ru. So `"uz"` can come only from `context_decision`: Uzbek in Latin script falls back to `"en"` and Uzbek in Cyrillic to `"ru"`. Unknown ⇒ `"en"`.

### A3. VALUES — capture, transport, validation, storage, stripping (D4)

#### A3.1 Sandbox side

Both exec sites start every job with fresh dicts:

| Site | Change |
|---|---|
| `code_exec._execute_in_process` env (`code_exec.py:231-269`) | add `"VALUES": {}, "FINDINGS": {}, "pdc_diag": pdc_diag` (`import pdc_diag` at `code_exec.py:57`, next to the `outlier_utils` import at `:56`) |
| `code_exec` capture (`code_exec.py:314-327`) | `outputs = exec_transport.capture_outputs(env)`; the success dict becomes `{"error": None, "result": …, "preview": …, "image_base64": …, **outputs}` |
| `plot_utils.GLOBAL_PLOT_SCOPE` (`plot_utils.py:360-404`) | add `"pdc_diag": pdc_diag` (module import next to `plot_utils.py:21`); VALUES/FINDINGS are NOT added to the shared scope |
| `plot_utils._render_in_process` (defined at `plot_utils.py:1894`; the `env =` line is `:1916`) | after `env = dict(GLOBAL_PLOT_SCOPE)`: `env["VALUES"] = {}; env["FINDINGS"] = {}` |
| `plot_utils._render_in_process` success returns (`:2001`, `:2011`, `:2201`, `:2211`) | each returns `_with_outputs(out, env)`, which merges `exec_transport.capture_outputs(env)` (lazy import inside the function, like `exec_sanitizer` at `:1919`); error returns unchanged |

`exec_transport.py` additions (shared by both images):

```python
VALUE_KEY_RE   = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}\Z")
FINDING_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
VALUES_MAX_KEYS = 40; VALUES_STR_MAX = 200
FINDINGS_PREFILTER_MAX_KEYS = 32; FINDINGS_STR_MAX = 64

def encode_values(raw) -> tuple[dict, int]      # (kept, dropped)
def encode_findings(raw) -> tuple[dict, int]
def capture_outputs(env: dict) -> dict          # {"values", "values_dropped", "findings", "findings_dropped"}
def decode_values(raw) -> tuple[dict, int]      # main app, hostile input, never raises
def decode_findings(raw) -> tuple[dict, int]    # main app, shape only (semantic check in narrative.validate_findings)
```

**`encode_values` / `decode_values`** (identical rules):
- `raw` must be a dict, else `({}, 0)`, or `({}, 1)` when raw is not None and not a dict.
- On the sandbox side (`encode_values` / `encode_findings`) a pdc_diag result dict is accepted too — an object whose type IS `pdc_diag._ResultDict` (type identity, looked up by a lazy `import pdc_diag` inside `exec_transport._pdc_result_type`, resolved ONCE per process and cached; both exec sites call it first, before the job's code runs — each job is a fresh runner process — so a module `__getattr__` the code installs with `setattr(pdc_diag, …)` never runs in the capture; unavailable → exact `dict` only and one `PDC_DIAG_RESULT_TYPE_UNAVAILABLE` line) — so `VALUES = r["values"]` / `FINDINGS = r["findings"]` are captured whole; it is read through `dict.items` like an exact dict, so none of its methods runs. Any other dict subclass, a subclass of `_ResultDict` included, is still one drop. `decode_values` / `decode_findings` (the main app, plain JSON dicts) stay exact-`dict` only.
- Iterate the items in insertion order. The key must be a `str` matching `VALUE_KEY_RE`.
- Value conversion:
  - a non-builtin with a callable `.item()` (a numpy scalar) is replaced by `.item()`;
  - accepted: `bool`, `int`, `float` (NaN/±inf kept; `fmt` shows `"n/a"`), `None`, and a `str` of `len ≤ 200`;
  - anything else (Timestamp, list, dict, DataFrame, a str over 200, Decimal) is dropped and counted.
- At most 40 accepted keys are kept; the rest are counted as dropped.
- Never raises; on an exception it returns `({}, n_seen)`.

**`encode_findings` / `decode_findings`:**
- the key must match `FINDING_KEY_RE`;
- the value is a `bool` (a numpy bool via `.item()`) or a `str` of 1–64 chars with no C0 control;
- at most 32 keys; everything else is dropped and counted.

**`serialize_result`** (`exec_transport.py:856-888`) already copies unknown keys verbatim, and all four keys are plain JSON, so it needs no change.

**`_reconstruct`** (`exec_transport.py:993-1017`):
- `_PYTHON_KEYS` (`:916`) and `_PLOT_KEYS` (`:917`) gain `"values", "values_dropped", "findings", "findings_dropped"`;
- `values` → `decode_values`, its drops added to `values_dropped`;
- `findings` → `decode_findings`, its drops added to `findings_dropped`;
- `*_dropped` → `known_number(value, 100_000)` (a non-int gives 0);
- these keys never cause a `_Violation`; a hostile `values` degrades to `{}`.

#### A3.2 Main app

- **Merge order.** After every successful block (`run_chat_local`), `exec_out["values"]` goes through `narrative.merge_values(state.values, …)` in block order; the later block wins.
- **Drop logging.** When `values_dropped + findings_dropped > 0`, one log line: `VALUES_DROPPED n=<n>` / `FINDINGS_DROPPED n=<n>`.
- **Which blocks count.** VALUES of a block that executed successfully are merged even when its chart is later dropped by dedup or the degenerate skip (`run_chat_local.py:1900-1923`). VALUES of a block that failed every retry are never merged; its slots render `"n/a"`.
- **Implicit result slot.** In the single flow, if `_safe_preview(exec_out["preview"])` is a `str`/`int`/`float`/`bool`, then `values.setdefault("result", that)`.
- **Storage.** Record field `values` (merged phase-1 VALUES) and `phase2.values`. They are made JSON-safe by the existing `local_store._json_safe` on `append_history`.
- **Stripping.** `brain_client._sanitize_history_rows` (`brain_client.py:194-217`) is an allowlist (`role`, `content`, `code`), so `values`, `findings`, `narrative_*`, `caption_*`, `value_display`, `phase2` and `table_captions*` are dropped automatically. Pinning tests are in C12.

### A4. `fmt(value, kind, locale=None)` — in `pdc_diag.py` (D5)

```python
_LOCALES = {"default": {"thousands": ",", "decimal": ".", "mln": "mln", "bln": "bln", "trln": "trln"}}
FMT_MAX_LEN = 32
def fmt(value, kind: str | None = None, locale: str | None = None) -> str   # never raises; exception -> "n/a"
```

Rules, applied in order:
1. `value is None` ⇒ `"n/a"`.
2. A numpy scalar ⇒ `.item()`.
3. `isinstance(value, bool)` ⇒ `"true"` / `"false"` for any kind.
4. A kind that is not in `KINDS`, or None ⇒ the default kind for the type (int → count, float → float, str → text).
5. Kind `text` / `period`:
   - a `str` ⇒ the value, first 200 chars;
   - an `int`/`float` ⇒ formatted with its type's default kind.
6. A numeric kind with a non-numeric value ⇒ the text rule.
7. NaN / ±inf ⇒ `"n/a"`.
8. Formatting always uses `format(v, ",.Nf")` (never scientific; Python rounds half-to-even on the binary value):
   - `count`: an `int` → `format(v, ",")` (exact, with no float conversion); a `float` → `",.0f"`;
   - `float`: `",.2f"`;
   - `pct`: `format(v * 100, ",.1f") + "%"` — the input is a fraction;
   - `ratio`: `0 ≤ v < 0.05` ⇒ `"<0.1x"`; `v > 1000` ⇒ `">1000x"`; otherwise `",.1f"` + `"x"` (a negative ratio keeps the fixed-point rule). A collapse such as 6.6e-6 therefore never reads as `"0.0x"`;
   - `amount`: `|v| ≥ 1e12` ⇒ `format(v / 1e12, ",.2f") + " " + trln` (decision F12); `|v| ≥ 1e9` ⇒ `format(v / 1e9, ",.2f") + " " + bln`; `|v| ≥ 1e6` ⇒ the same with `/1e6` and `mln`; otherwise the count rule.
9. Negative zero: if the result starts with `"-"` and contains no digit 1–9, drop the `"-"`.
10. Locale: when `locale` names an entry other than `default`, swap the separators through a placeholder (`,` → `\0`, `.` → decimal, `\0` → thousands) and swap the `mln`/`bln`/`trln` words. An unknown locale ⇒ default. Two locales exist besides `default` (decision F20, 2026-10-25): `uz` = thousands U+00A0 (no-break space), decimal `,`, words `mln` / `mlrd` / `trln`; `ru` = the same separators, words `млн` / `млрд` / `трлн`. `narrative` picks the locale from the answer language (`locale_for(lang)`: uz → `uz`, ru → `ru`, everything else default) for every slot, caption, `value_display` line and finding sentence. Period labels are shown in the file's own form (month names are not translated).
11. A result longer than `FMT_MAX_LEN` (32) ⇒ `"n/a"`.

**Example table** (pinned by `tests/test_pdc_diag_fmt.py::test_fmt_examples_table`; every magnitude is invented):

| value | kind | output |
|---|---|---|
| `1234567` | count | `1,234,567` |
| `1234567.4` | count | `1,234,567` |
| `2.5` | count | `2` |
| `3.5` | count | `4` |
| `-1234` | count | `-1,234` |
| `0` | count | `0` |
| `1234.5678` | float | `1,234.57` |
| `3` | float | `3.00` |
| `5e-7` | float | `0.00` |
| `-0.001` | float | `0.00` |
| `0.375` | pct | `37.5%` |
| `0.1234` | pct | `12.3%` |
| `1` | pct | `100.0%` |
| `-0.031` | pct | `-3.1%` |
| `12.5` | pct | `1,250.0%` |
| `3.27` | ratio | `3.3x` |
| `12` | ratio | `12.0x` |
| `6.6e-6` | ratio | `<0.1x` |
| `1e9` | ratio | `>1000x` |
| `1234567` | count, `uz` / `ru` | `1 234 567` (U+00A0) |
| `2345678901` | amount, `uz` / `ru` | `2,35 mlrd` / `2,35 млрд` |
| `7250000` | amount, `uz` / `ru` | `7,25 mln` / `7,25 млн` |
| `5e12` | amount, `uz` / `ru` | `5,00 trln` / `5,00 трлн` |
| `0.375` | pct, `ru` | `37,5%` |
| `6.6e-6` | ratio, `ru` | `<0,1x` |
| `0.48` | ratio | `0.5x` |
| `2345678901` | amount | `2.35 bln` |
| `7250000` | amount | `7.25 mln` |
| `999999` | amount | `999,999` |
| `1500.6` | amount | `1,501` |
| `-3700000000` | amount | `-3.70 bln` |
| `5e12` | amount | `5.00 trln` |
| `7.5e15` | amount | `7,500.00 trln` |
| `"North"` | text | `North` |
| `"x" * 250` | text | the first 200 `x` |
| `"Q3"` | period | `Q3` |
| `7` | text | `7` |
| `0.25` | period | `0.25` |
| `None` | count | `n/a` |
| `float("nan")` | count | `n/a` |
| `float("inf")` | amount | `n/a` |
| `"abc"` | count | `abc` |
| `True` | count | `true` |
| `False` | text | `false` |
| `numpy.int64(42)` | count | `42` |
| `numpy.float64(0.25)` | pct | `25.0%` |
| `1e40` | count | `n/a` (longer than 32) |
| `123456789012345678` | count | `123,456,789,012,345,678` |
| `42` | `"bogus"` | `42` |
| `0.5` | None | `0.50` |

### A5. Rendering, storage and display (D7)

#### A5.1 Where rendering happens (`run_chat_local.py`)

```python
@dataclass
class _NarrState:                 # one per phase
    phase: int
    template: str | None          # narrative.clean_template(plan["narrative"], max_len=4000, ...)
    captions: dict                # {block_index: caption template}; narrative.clean_captions(plan["captions"])
    values: dict
    findings: dict
    input_columns: frozenset      # {str(c) for cols in df_columns.values() for c in cols}, computed ONCE at
                                  # run_chat_local.py:1570 (before the plan and before _ensure_live), i.e. the
                                  # registered/uploaded column names — never live-SELECT aliases or columns the code creates
    ok_blocks: int = 0
    block_findings: dict = field(default_factory=dict)   # {block_index: validated FINDINGS}, merged at phase end (B7.2)
```

- **Absorb.** `_NarrState.absorb(exec_out, code, block_index)` runs after each successful block. It calls `merge_values`, then sets `block_findings[block_index] = validate_findings(exec_out["findings"], code, input_columns, sid)`, then `ok_blocks += 1`. The name `df_columns` keeps its existing meaning (the `{df_name: [cols]}` dict sent on retry) and is never passed to the validator.
- **Captions.** Right after a block succeeds:
  - if caption `n` exists, `narrative.render(captions[n], block_values, block_findings[n], sid=sid, max_len=1200)` with the VALUES of block n ONLY. Table blocks run after all chart blocks, so the VALUES of a lower-indexed block may not exist yet.
  - A table block whose RESULT is a dict yields several tables: caption n goes on the first table and the others get `""`.
  - With the split-axes fallback (`run_chat_local.py:1812-1828`), caption n goes on sub-chart 0 and the others keep `cobj.title`.
  - if `result_backstop.inspect_outputs` fires for that visual, prepend `result_backstop.fallback_sentence(lang, …)` + `"\n\n"` to both the template and the rendered caption (a value-free sentence; no brain call);
  - if caption `n` is missing, today's per-chart `_describe_with_backstop` runs for that visual only (A6).
  - the rendered caption passes the quality gate below (`what=caption`); a caption the gate empties counts as missing, so describe runs for that visual.
- **Narrative.** After all blocks of the phase finish, and only if `ok_blocks ≥ 1`: `render(template, values, findings, sid=sid)`, then the quality gate (`what=narrative`), then `_append_live_notes` (`run_chat_local.py:478`) on the RENDERED text only. If `ok_blocks == 0`, the narrative is not used (`NARRATIVE_SKIPPED reason=no_successful_block`) and today's failure texts apply. If the gate empties the text, the narrative is not used either (`NARRATIVE_SKIPPED reason=all_sentences_dropped`) and the answer takes today's no-narrative path: the per-visual describe / caption texts, or summarize plus `value_display` for a scalar.
- **Quality gate** — `narrative.drop_na_sentences(text, *, sid, what) -> str`, applied to every rendered text before display. The text is split on newlines and after `.` / `!` / `?` followed by whitespace or the end (so `7.9x` or `1,234.57` is not a sentence end). Every piece holding the token `n/a` (case-insensitive, not inside a word) is removed, then the A2.2 space/newline post-processing runs again. A missing slot, a slot whose value is None and a non-finite value all render `n/a`, so a sentence about a value the code did not produce never reaches the user. One line `NARRATIVE_SENTENCE_DROPPED reason=missing_slot n=<pieces> what=<narrative|caption>` per text, never the text. The stored template and VALUES are unchanged; a text without `n/a` is returned as it is. Never raises. Known limits: a value or template text that legitimately reads `N/A` (a category named so) drops its sentence too, and is still logged as `missing_slot`; an abbreviation such as `e.g. ` counts as a sentence end.
- **Invalid template.** Before the gate, `finish` checks the template: a parse error (`NARRATIVE_PARSE_FAILED kind=nested|unclosed|mismatched`) or a FIRST rendered sentence holding `n/a` (`NARRATIVE_INVALID reason=first_sentence`) makes it invalid; the headline is never dropped silently and `fallback_text` is no longer shown as a narrative. An invalid template gets ONE narrative-only retry through `/v1/retry`: the flow's exec-error retry keywords with `error_msg = "narrative_invalid: <kind>"` (kind `nested`, `unclosed`, `mismatched` or `first_sentence`; when a SLOT made the first sentence show `n/a`, `"narrative_invalid: first_sentence slot=<name> cause=<missing|none|nan>"` — `narrative.first_sentence_na_slot` finds the first slot of the first sentence whose key is absent, whose value is None or whose value is non-finite, through a marking pass of `render_with_sections`; `narrative.invalid_error_msg` builds the string, and the same detail is logged `NARRATIVE_INVALID reason=first_sentence slot=<name> cause=<cause>`. The slot name is a template identifier, never a value; any other `n/a` — a literal one, a fmt overflow, an unsupported type — sends the plain kind), `failed_code` = the code that already ran (single: the block's code; multi: the emitted chart codes then the table codes, joined with `\n###NEXT_PLOT###\n`), `use_pro = true`, `sql` = what ran, `sql_error = null`. Only the reply's `narrative` is read (through `clean_template`); its code and captions are ignored and nothing re-runs (the SQL map is not merged). Its usage is added to the turn. The reply's template is checked the same way: valid ⇒ it replaces the plan's template (also in `content` = `history_content`), invalid or absent ⇒ `NARRATIVE_INVALID_TWICE` and the turn takes the no-narrative path (A6). A raise in the retry logs `NARRATIVE_RETRY_FAILED error=<Type>` and counts as no reply.
- **Retry time-box and progress.** The narrative-only retry passes `timeout = NARRATIVE_RETRY_TIMEOUT_S` (45 s) to `brain_client.retry` (the exec-error retries pass `timeout=None`, the client-wide default). A `BrainTimeoutError` logs `NARRATIVE_RETRY_TIMEOUT elapsed_ms=<n> timeout_s=45` and counts as no reply, so the turn describes as today. Right before the call, the flow emits `{"progress": True, "message_key": "lab.narrative_retry", "message": "Writing the summary…"}` through the `on_progress` callback of `run_chat_multi_plot` (threaded to the single flow); the stream worker enqueues it for the SSE consumer (A5.4). A raising callback logs `NARRATIVE_PROGRESS_FAILED error=<Type>` and is ignored. Edit-regenerate passes no callback.
- **Headline fallback.** When the template is dropped after the retry (every `NARRATIVE_INVALID_TWICE` path, a timeout included) and VALUES holds `result`, `total` or a key ending `_total` (in that order, the first whose value is a finite int or float), `narrative.headline_sentence` builds ONE fixed sentence in the answer language, `f"{_RESULT_LABEL[lang]}: {fmt(v, 'amount', locale)}."` (uz: `Natija: 61,58 trln.`), logged `NARRATIVE_HEADLINE_FALLBACK key=<name> lang=<code>` (the key name only). Multi flow: a `{"narrative": True, "phase": 1, "text": <headline>}` event before done (it fills the phase-1 slot above the charts), `display_answer` starts with it, `narrative_view.headline_display` carries it. Single flow: `headline_display` on the result, only when there is no `value_display` (that line already shows the figure). Display text only (R0): never in `content` / `combined_answer` or any brain payload; the record keeps it as `headline_display` (A5.5) and reload shows it first. No key ⇒ today's answer.
- **Finding sentences** (`narrative.FINDING_SENTENCES`, `add_finding_sentences`). After the gate, `finish` appends one fixed sentence, in the answer language (en / uz / ru / ka from `answer_lang_code`; unknown ⇒ en), for each data-quality finding present in the merged FINDINGS that no shown section already states. A section states a finding when it was shown as `{{#<code>}}` (after the prefix resolution of A2.2, never `{{^…}}`) and one of its body sentences survived the gate. Findings, in this order: `mixed_units` (`units == "mixed_units"`; covered by `mixed_units`, `units`), `coverage_change` (`coverage == "coverage_change"`; covered by `coverage_change`; slot `coverage_dim_label`, else the `coverage_dim` finding), `label_disappeared` (`label_disappeared` true; covered by itself; slots `disappeared_label` + `disappeared_after_period`), thin period / data gap (`thin_period` or `period_status == "data_gap"`; covered by `thin_period`, `data_gap`; slot `thin_period_label`, else `data_gap_period`), `scope_change` (`anomaly == "scope_change"` or `change_shape == "scope_change"`; covered by `scope_change`; slots `anomaly_period`, else `change_to_period`, + `rows_ratio` formatted as a ratio), `unknown_dominated` (covered by `unknown_dominated`; slots `unknown_dim_label` + `unknown_share_max` only when that label equals the `unknown_dim` finding or no `unknown_dim` is present, else the `unknown_dim` finding alone without a share — so the dimension and its share always come from one result; BRB run 3 en q3 showed the SEKTOR finding with another call's SEGMENT share), `metric_definition_overlap` (`metric_relation`; covered by `metric_definition_overlap`, `metrics_identical`, `metric_relation`; slot `overlap_equal_share`), `latest_is_past_period` (covered by itself; slot `latest_period`). A slot is looked up by its exact VALUES key, else the first key `^[a-z][a-z0-9]{0,9}_<name>$` (prefixed VALUES). Every value is formatted with the answer locale and lives in a parenthesis; a missing, None or `n/a` value drops its part, and with no part left the whole parenthesis goes, so a finding sentence never shows `n/a`. `scope_change` and `label_disappeared` also carry a FULL form with the values inline (`FINDING_SENTENCES[code][lang]` is then `(base, paren, full)`), used only when every value it names is present; otherwise their value-free base sentence follows the parenthesis rule. The sentences form ONE new paragraph: before the last sentence when that sentence is a reading guide (it names a visual: en table/chart/graph/bar/waterfall, uz jadval/grafik/diagramma, ru таблиц/график/диаграмм, ka ცხრილ/გრაფიკ/დიაგრამ; and it is not the first sentence), else at the end. Logged `FINDING_SENTENCES_ADDED n=<n> lang=<code>`. Display text only (R0): `content` stays `history_content(template, findings)`. English wording: "Amounts carry more than one currency or unit label, so they are reported per label and never added together." / "The set of values in one dimension changes between periods (CURRENCY), so totals are not comparable across periods." / "One period looks incomplete (2026 Февраль): it has far fewer rows or almost no value compared with the other periods, so it is treated as a data gap, not a business change." (one wording for both causes: the row rule and the value collapse) / "Many rows have no value in one dimension (SEKTOR; up to 100.0% Unknown), so that dimension cannot explain any change." / "In most rows this metric equals another metric of the data (97.6% of rows), so it counts a stock, not new items." / "The data ends before the current month (latest period: 2026 Март)." / full forms: "Rows labelled Ўзбек Сўми (UZS) end after 2025 Декабрь; later periods carry only the other label(s), so a 0 for Ўзбек Сўми (UZS) in a later period means no rows, not no activity." / "2026 Март has 8.8x the rows of the previous period, so its totals reflect a change in reporting scope, not a business change; compare rates or per-row averages instead." (order: mixed_units, coverage_change, label_disappeared, thin period, scope_change, unknown_dominated, metric_definition_overlap, latest_is_past_period; uz / ru / ka and the value-free base forms: `narrative.FINDING_SENTENCES`).
- **Code gate** (BRB run 3, en q4 showed raw Python). `narrative.contains_code(text) -> bool` is True for a ``` fence, or for three consecutive non-blank lines that parse as Python (`ast.parse`) and hold a STRONG construct (an import, def / class, for / while / with / try / return, a lambda, a comprehension or generator, a dict literal, a method call, a call with keyword arguments or a string subscript such as `dfs['x']`); an assignment, an `if` or a call of a plain name alone does not count, so label lists (`Revenue (USD)`), formula lines (`Margin = Revenue - Cost`) and `Q1 = 5` lists are prose; an annotation (`Result: 5`), bare names, numbers and prose never count; never raises. `narrative.code_in_text(text, *, sid, what)` adds one `NARRATIVE_CODE_IN_TEXT what=<answer|caption|narrative>` line, never the text. After the `n/a` gate, a caption holding code counts as missing (`CAPTION_SKIPPED block=<n> reason=code_in_text`, describe runs for that visual) and a narrative holding code is not used (`NARRATIVE_SKIPPED reason=code_in_text`, the no-narrative path). A PROSE reply (plan kind CLARIFICATION / ANSWER / MISSING_DATA, whose text is shown as is) that holds code is never shown: `run_chat_local._code_from_prose_reply(text, sid) -> (kind, code) | None` takes the first ```python / ```py fence that parses (however short: the tag declares it code) or the first bare ``` fence whose content `contains_code` accepts, or — for a text with no fence at all — the whole text when it parses and `contains_code` accepts it; anything else is None and the prose is shown (a ```sql or ```json explanation stays an answer). Kind PLOT_CODE when it assigns `fig` and not `RESULT`, else PYTHON, and the turn logs `NARRATIVE_CODE_IN_TEXT what=answer` once and runs it on the normal path (`run_chat` and the single flow; retry loop on failure, describe / summarize for the text — no narrative, the brain drops it for a prose kind). A prose reply without runnable Python is shown as before. Only the first plan reply is searched: a prose-kind RETRY reply stays a failed attempt.
- Log after each phase: `NARRATIVE_USED phase=<p> slots=<n> missing=<n> captions=<n> described=<n>`.

#### A5.2 Brain-safe text vs display text — one rule

> **R0.** A string produced by `narrative.render` or `narrative.value_display` is never stored in `content`, never passed to a `brain_client.*` function, and never placed in a field read by `/v1/title` / `/v1/report`. Brain-bound text is built only from templates (`history_content`, `brain_view`), describe/summarize replies and fixed client sentences.

#### A5.3 Single-flow result dict (`_run_single_body`, `run_chat_local.py:2082-2193`)

Existing keys stay. These keys are added, only when present:

| Key | Content |
|---|---|
| `text` | brain-safe. Narrative used ⇒ `narrative.history_content(template, findings)`. No narrative but caption 1 present ⇒ `caption_template` (never the rendered caption, per R0). Otherwise the describe/summarize text, as today |
| `narrative_template`, `narrative_rendered` | phase-1 narrative |
| `values`, `findings` | merged VALUES / validated FINDINGS |
| `caption_template`, `caption_rendered` | caption 1 of the single visual (image or table) |
| `caption_described` | the describe text for the single visual when a narrative exists but caption 1 is missing (A6) |
| `value_display` | the local value line(s) of the fallback (A6.4); only when there is no narrative and the result is a scalar or a flat dict |

#### A5.4 Generator events (`run_chat_multi_plot`)

| Event | Keys added (internal) | Forwarded to the browser by the `routes/chat.py` consumer |
|---|---|---|
| chart partial (existing, `run_chat_local.py:1822-1828`, `:1948-1954`) | `answer` = rendered caption or describe text, `caption_template` (or None), `phase`, `block_index` | existing payload (`routes/chat.py:1866-1879`) + `phase` |
| tables partial (NEW; phase 1, before phase 2 only) | `{"partial": True, "phase": 1, "tables", "table_codes", "table_result_keys", "table_captions", "table_caption_templates"}` | `{partial, phase, tables, full_table_keys, table_captions, conv_id}` |
| narrative (NEW) | `{"narrative": True, "phase": p, "text": rendered, "template", "values", "findings"}` | `{narrative: true, phase, text, conv_id}` |
| progress (NEW, phase 2) | `{"progress": True, "phase": 2, "message_key": "lab.phase2_progress", "message": "Checking what drives this…"}` | the same + `conv_id` |
| progress (narrative retry, A5.1) | `{"progress": True, "message_key": "lab.narrative_retry", "message": "Writing the summary…"}` — through the `on_progress` callback (single and multi flow), not yielded | the same + `conv_id` |
| narrative (headline fallback, A5.1) | `{"narrative": True, "phase": 1, "text": <headline>}` — no `template` | `{narrative: true, phase: 1, text, conv_id}` |
| done (existing, `run_chat_local.py:2038-2049`) | `combined_answer` = brain-safe (below), `display_answer`, the tables not yet emitted with their aligned `table_captions`, `table_caption_templates`, `table_phases`, and `narrative_view` = `{"phase1": {...} or None, "phase2": {...} or None, "phase2_status": str or None}` | existing + `answer` = `display_answer`, `record_view` = `{narrative_rendered, phase2: {narrative_rendered}, phase2_status}` |

- **`combined_answer`** (brain-safe):
  - when the phase-1 narrative is used: `history_content(t1, f1)`, plus `"\n\n" + history_content(t2, f2)` when phase 2 succeeded;
  - otherwise today's join, where each chart contributes its `caption_template` when it has a caption, else its describe text.
- **`display_answer`**: the rendered narrative(s) when used; otherwise today's combined text built from rendered captions or describe texts.
- **Consumer routing in `routes/chat.py` `_sse_generator`.** Today `:1855` takes every `partial` event as a chart, and any other event falls into the single-shot block at `:1910-1944`, which emits `done`. The new rule:
  1. `ev.get("narrative")`, `ev.get("progress")`, and a tables partial (`ev.get("partial") and not ev.get("image_base64") and ev.get("tables")`) are each handled in their own branch, BEFORE `if ev.get("partial")`.
  2. The single-shot block runs only when `ev.get("single_response")`.
  3. Any other event is dropped and logged `SSE_EVENT_UNKNOWN keys=<sorted key names>`.
  4. The worker (`:1693-1765`) uses the same predicates.

  Tests: `tests/test_chat_stream_phases.py::test_narrative_event_does_not_end_stream` and `::test_tables_partial_not_rendered_as_chart`. Session 1 ships the first as `tests/test_narrative_sse.py::test_narrative_event_does_not_end_stream`; `tests/test_chat_stream_phases.py` belongs to session 3.

#### A5.5 Record fields (`routes/chat.py`)

**Builders.** `_build_ai_history_record` (`:508-579`), `_build_stopped_record` (`:582-627`) and the two edit-regenerate record sites (`:2110-2142`, `:2183-2199`) all call one new helper:

```python
def _apply_narrative_fields(rec: dict, src: dict | None) -> dict
```

`src` is the single result dict, or the done event's `narrative_view` plus the table arrays. Only keys that are present are written, so an old-shape turn keeps today's shape.

| Field | Type | When |
|---|---|---|
| `content` | str | brain-safe (A5.4 / A5.3); unchanged meaning for turns without a template |
| `narrative_template` / `narrative_rendered` | str | phase-1 narrative used |
| `values` / `findings` | dict | phase-1 narrative or FINDINGS present |
| `caption_template` / `caption_rendered` | str | single-visual record with a caption |
| `caption_described` | str | single-visual record, narrative present, caption missing (describe text) |
| `images[i].answer` | str | rendered caption when the entry has `caption_template`; otherwise the describe text (today) |
| `images[i].caption_template` | str | captioned chart |
| `images[i].phase` | 1 \| 2 | always, on turns run with `narrative_v1` |
| `table_captions` / `table_caption_templates` / `table_phases` | list aligned with `tables` | mixed/table turns with `narrative_v1` |
| `value_display` | str | fallback scalar/dict display (A6.4) |
| `headline_display` | str | the headline fallback sentence (A5.1), display-only; single result key or `narrative_view.headline_display` |
| `phase2` | `{narrative_template, narrative_rendered, values, findings}` | phase 2 succeeded |
| `phase2_status` | `"done"` \| `"failed:<reason>"` | phase 2 attempted |

**Builder rules:**
- `_build_chart_entry` (`:480-490`) copies `caption_template` and `phase` when present.
- The record uses `images[]` when there are ≥2 images OR any image has `phase == 2`. Reload handles a 1-entry array.
- `_build_stopped_record`:
  - `content` is built from caption templates and describe texts, NEVER from `answer` (rendered). After the phase-1 narrative event, `content = history_content(t1, f1) + "\n\n⏹ Response stopped by user."`;
  - the cancel-synthesized done event (`routes/chat.py:1760`) sends a `display_answer` built from the rendered answers to the browser, never `combined_answer`;
  - `done.narrative_view` is authoritative, and `w_narr` is used only for stopped records;
  - the phase-1 narrative fields are written when the worker saw the phase-1 narrative event;
  - `phase2_status = "failed:stopped"` when stopped after the phase-2 progress event.

**Worker accumulation** (`routes/chat.py:1724-1741`):
- `w_charts` entries keep `caption_template` and `phase`.
- New `w_narr = {1: ev, 2: ev}` collects the narrative events.
- Full-table persistence (`:1693-1706`) runs for any non-single event that carries `tables` + `table_codes` (partial or done); the keys are appended to `w_full_table_keys` and the tables to `w_combined_tables`.
- The cancel synthesis (`:1755-1765`) builds `w_combined_answer` from `caption_template or answer`, where `answer` is used only for entries without `caption_template`.

**Edit-regenerate** (`routes/chat.py:2048-2206`):
- It collects `tables`, `table_codes`, `table_result_keys` and `table_captions` from tables partials exactly as the stream worker does. Today `:2102-2107` skips image-less partials, and that must change.
- It persists them with `_persist_full_table`.
- It answers `{record_view, answer: display_answer}`.
- It never sends `combined_answer` or `result.text` as `answer`; at `:2145` and `:2206` today that would be the raw marker and template.
- It runs the generator with `allow_phase2=False` (B9.1).

**Title.** `_start_title_generation` (`:2249-2274`) is called with `answer_for_title`: `narrative.brain_view(t1, f1)` when a narrative exists, else `content`. For summarize fallbacks, `content` no longer contains a value.

**Report** (`routes/report.py`):
- `_build_qa_pairs` (`:220-255`) sets `pair["ai"]["content"]` to the brain-safe text and `pair["ai"]["_display_text"]` to the rendered text:
  - new records with a narrative: content = `brain_view(narrative_template, findings)` (+ `brain_view(caption_template)`), display = `narrative_rendered` (+ caption);
  - image entries: content = `brain_view(img.caption_template, record.findings)` when present, else `img.answer`; display = `img.answer`;
  - the first image pair of a record carries the narrative prefix;
  - phase-2 pairs carry `phase2.*` the same way.
- `_build_findings_for_brain` (`:263-277`) keeps reading `content`, with one change. `table_columns` (`:274`) keeps only names in the chat's input column set (from `schema_docs`). Any other result-table label (pivot or crosstab headers can be category values or dates) is removed, and the field may become `[]`. Test: `test_report_table_columns_only_input_names`.
- Per-finding body in the local PDF/PPTX: `pair["ai"].get("_display_text")` when present. The brain still supplies the page titles and the executive summary, so a rendered report shows real numbers and no `[slot]` placeholders. The same `_display_text or content` rule applies at the four fallback sites (`:570`, `:1252`, `:2155`, `:2270`).
- Text inserted into ReportLab `Paragraph` (`report.py:2272` and siblings) passes `xml.sax.saxutils.escape` first, because VALUES text may come from a cell.

#### A5.6 Display (`static/dashboard.js`)

**`renderAiRecord(msg, convId, rowIdx)`** is new. It is extracted from the reload loop (`dashboard.js:1939-1967`) and used by reload, `_tryRecoverResponse` (`:3104-3145`) and edit-regenerate (`:3588-3640`, which now receives `record_view` = the persisted record). It renders in this order:
1. `msg.narrative_rendered` ⇒ `appendNarrativeMessage(msg.narrative_rendered, 1)`; otherwise `msg.headline_display` ⇒ `appendNarrativeMessage(msg.headline_display, 1)`.
2. Phase-1 visuals:
   - `images` entries with `phase !== 2` ⇒ `appendMessage('assistant', img.answer || '', img.image_base64, …)` (as today);
   - otherwise a single visual ⇒ text = `msg.narrative_rendered !== undefined ? (msg.caption_rendered || msg.caption_described || '') : (msg.caption_rendered ?? (msg.value_display ? msg.content + '\n\n' + msg.value_display : msg.content))`.
3. Phase-1 tables (`table_phases[i] !== 2`), each preceded by its `table_captions[i]`.
4. `msg.phase2?.narrative_rendered` ⇒ `appendNarrativeMessage(…, 2)`.
5. Phase-2 images and tables.
6. Old records (none of the new keys) render exactly as today.

**`appendNarrativeMessage(text, phase)`** creates `div.message.assistant.pdc-narrative[data-phase]` with a `.message-content` whose `innerHTML = renderMarkdown(text)`. `renderMarkdown` escapes first.

**Table captions.** `appendMessage` takes `extras.tableCaptions`, an array aligned with `extras.tables`, and renders a `div.pdc-caption` (via `renderMarkdown`) above each `.pdc-table-block` (`:2429-2448`).

**Live stream** (`sendMessage`, `:3148-`). Check `data.narrative` and `data.progress && data.phase` BEFORE `data.partial` (`:3285`). A tables partial is detected by `data.partial && !data.image_base64 && data.tables`; it calls `clearTimeout(timeoutId)` like a chart partial, but does not increment `chartCount`.
- At send: `slot1 = _createNarrativeSlot(1)`, a hidden `.pdc-narrative[data-phase="1"]` inserted before the loading bubble.
- On `data.narrative`: fill `slot[data.phase]` via `renderMarkdown` and unhide it.
- On `data.progress && data.phase === 2`: remove `streamLoadingDiv`, append `slot2` (hidden), then append a status bubble with the text `_t(data.message_key, data.message)`.
- On `data.partial && !data.image_base64 && data.tables`: append the tables message with captions. Do not increment `chartCount`.
- Single done (`chartCount === 0`): if `data.narrative`, call `appendNarrativeMessage(data.narrative, 1)` and then `appendMessage(data.caption || '', image, table, …)`; otherwise behave as today.
- At done or stop: remove any slots that are still hidden.

**CSS and i18n:**
- `static/dashboard.css`: `.pdc-narrative .message-content` and `.pdc-caption` (margin only; no new colours).
- `static/i18n.js` (en/geo/ru): `lab.phase2_progress`, `lab.narrative_retry` ("Writing the summary…"). The generic progress branch of the live stream shows `_t(message_key, message)` for any keyed progress event: in the loading bubble while it is visible, else in a status bubble that `done` removes.

### A6. Fallbacks

| Situation | Behaviour |
|---|---|
| No narrative (old brain, model omitted it, dropped by caps or kind) | today's describe per visual (with `data_caveat.findings` when FINDINGS exist, B7.4); summarize for a scalar with `preview=None` + local `value_display` |
| Narrative present, a visual has no caption | user item A.5: today's per-chart describe for THAT visual only (unchanged prompt; `data_caveat.findings` added when FINDINGS exist; backstop caveat as today). The answer-level text stays the narrative; summarize is never called. See F27 |
| No narrative, some captions present | captioned visuals use the rendered caption (no describe); uncaptioned visuals → describe with an unchanged prompt (+ findings caveat) |
| Render error (caption) | `fallback_text` (sections removed, slots `"n/a"`), `NARRATIVE_RENDER_FAILED error=<Type>`; the quality gate then drops every sentence that held a slot |
| Narrative template invalid (parse error, or a first sentence holding `n/a`) | ONE narrative-only retry (`narrative_invalid: <kind>`, with `slot=<name> cause=<missing\|none\|nan>` for a slot, A5.1), time-boxed at 45 s; its valid narrative is rendered, else `NARRATIVE_INVALID_TWICE` and the "No narrative" row, preceded by the headline fallback sentence when VALUES holds `result` / `total` / `*_total` |
| Narrative-only retry exceeds 45 s | `NARRATIVE_RETRY_TIMEOUT`; counts as no reply (the row above) |
| A finding no shown section states | one fixed sentence per finding, in the answer language (A5.1) |
| A rendered sentence holds `n/a` (missing slot, None or non-finite value) | that sentence is dropped (A5.1 quality gate); a caption left empty ⇒ describe for that visual; a narrative left empty ⇒ the "No narrative" row |
| A rendered narrative or caption holds code (A5.1 code gate) | the caption counts as missing (describe for that visual); the narrative is not used (the "No narrative" row) |
| A prose-kind reply (CLARIFICATION / ANSWER / MISSING_DATA) holds runnable Python (a python / py / bare fence, or a fence-free text that parses as code) | its code is extracted and run on the normal path; describe / summarize write the text (A5.1) |
| Every block of the phase failed | narrative not used; today's failure texts |
| Summarize fallback | `brain_client.summarize(..., preview=None)` at `run_chat_local.py:981` and `:2187`; then `value_display = narrative.value_display(_safe_preview(preview), lang)` is appended to the display only |

**A6.4 `value_display`:**
- a scalar ⇒ `f"**{_RESULT_LABEL[lang]}:** {fmt(v)}"`;
- a flat dict (the `_safe_preview` shape) ⇒ up to 20 lines `f"- {str(k)[:80]}: {fmt(v)}"`, then `"- …"` when there are more;
- `_RESULT_LABEL = {"en": "Result", "ka": "შედეგი", "ru": "Результат", "uz": "Natija"}`;
- log `VALUE_DISPLAY_APPENDED kind=scalar|dict`.

---

## B. FINDINGS and diagnostics

### B7. FINDINGS (D6)

#### B7.1 Validator — `narrative.validate_findings(raw, code, input_columns, sid) -> dict`

Applied per block, against that block's executed code:
1. `raw` must be a dict (it already passed `decode_findings`).
2. The key matches `^[a-z][a-z0-9_]{0,39}$`.
3. The key is allowed only if it appears literally as a quoted string in `code` (`re.search(r"(['\"])" + re.escape(key) + r"\1", code)`) OR it is in `pdc_diag.PDC_DIAG_FINDING_KEYS`. A key built from data (`f"is_{region}"`) never appears literally, so it is dropped.
4. The value is a `bool`, OR a `str` equal (case-sensitive) to a member of `input_columns` (A5.1 — the input frames' registered/uploaded column names, never aliases or derived columns), OR a `str` in `pdc_diag.FINDINGS_ENUM`. Anything else is dropped.
5. The reserved key `needs_phase2` must be a `bool`.
6. The value must not contain `;`, `]`, `\r`, `\n`, U+2028 or U+2029. A column name that contains one is dropped, so the marker grammar (B7.3) cannot be broken.
7. The cap is applied only at merge time (B7.2), never per block.
7. Drops are counted and logged as `FINDINGS_DROPPED n=` (no names, no values).

ENUM v1:

```python
FINDINGS_ENUM = ("coverage_change", "coverage_stable", "partial_period", "metric_definition_overlap",
                 "mixed_units", "concentrated_in_one_dim", "spread_evenly", "weak_signal",
                 "dominant_feature", "anomaly_confirmed", "no_anomaly",
                 "data_gap", "outlier_driven", "seasonal_pattern",
                 "scope_change", "label_disappeared")   # appended (BRB run 3)
# needs_phase2 is NOT a value (decision F14); it is only the reserved boolean KEY.
# scope_change is emitted by detect_anomalies as anomaly="scope_change" and by
# decompose_change as change_shape="scope_change" (B8.4, B8.5); label_disappeared is a
# boolean coverage_check finding (B8.3), listed here as a code so {{#label_disappeared}}
# reads like the other codes.
# data_gap is emitted by detect_anomalies as period_status="data_gap" (B8.4).
# outlier_driven / seasonal_pattern are not emitted by pdc_diag v1; planner code may set them
# (e.g. FINDINGS["pattern"] = "seasonal_pattern") from its own checks.
```

#### B7.2 Merge

Each block's validated FINDINGS are kept until its phase completes. They are then merged in `block_index` order, NOT execution order: table blocks run after chart blocks (`run_chat_local.py:1636-1638`, `:1963`), and execution order would let the cap cut block 1's coverage findings.
- A later `block_index` wins, logged as `FINDINGS_KEY_COLLISION n=`.
- The cap `FINDINGS_MAX_KEYS = 24` is applied with this priority: `needs_phase2` first, then `pdc_diag.PDC_DIAG_FINDING_KEYS` members in block_index order, then planner-literal keys in block_index order.
- The worst case per phase (coverage_check 11 + detect_anomalies 5 + check_metric_overlap 2 + needs_phase2 1 = 19 registry keys) fits under 24.

Phase-2 FINDINGS are validated against phase-2 code and merged only into `phase2.findings`.

#### B7.3 History marker — `narrative.findings_marker`

```
[Findings: anomaly=anomaly_confirmed; coverage_dim=CURRENCY; needs_phase2=true]
```

- Keys are sorted. Bools are written `true`/`false`; columns and codes as they are. The separator is `"; "`.
- Empty FINDINGS ⇒ `""` (no marker line).
- Cap 400 chars: keys are added in sorted order while the line, including `"; …]"`, stays ≤ 400.
- `history_content(t, f)` = `marker + "\n" + t`. The marker goes FIRST because the brain trims history to `PROMPT_HISTORY_TRIM_CHARS` = 800 (`PDC_Brain/settings.py:39`) and the classifier trims it to 150 chars (`brain_agent.py:1061`).

#### B7.4 Describe fallback extension

`run_chat_local._describe_with_backstop` (`run_chat_local.py:1035-1085`) gets a new kwarg `findings: dict | None = None`:
- no detection and no findings ⇒ a plain describe (today);
- findings only ⇒ `brain_client.describe(..., data_caveat={"findings": findings})`, no marker logic;
- a detection ⇒ today's caveat with `"findings"` added.

`brain_client._compact_caveat_for_transport` (`brain_client.py:332-355`) must change. Today it returns `None` at `:340-342` when `kind` is unknown, so a findings-only caveat would never reach the brain. New logic:
- start with `out = {}`;
- if `kind in _CAVEAT_KINDS`, copy `kind`, `facts`, `grain` and `catalog` exactly as today;
- an unknown or absent `kind` drops only those keys, never `findings`;
- re-validate `findings` for shape (key regex, bool or str ≤ 64 without controls or `;]`, ≤ 24 keys);
- return `out` when it holds (`kind` with `facts` or `grain`) or `findings`; otherwise return `None`.

Tests: `test_compact_caveat_findings_without_kind_kept` and `test_compact_caveat_unknown_kind_without_findings_still_none`.

Constant-value hints are removed at the source for every describe call, narrative or not (decision F1, B7.6): `facts` never carry a value.

The brain's `_findings_block` text is in B10.7.

#### B7.6 Value scrubs in existing payloads (decision F1 — session 1)

- **`data_caveat` facts.** `result_backstop` stops appending `_fmt_value` constant-value hints to `facts`
  (`result_backstop.py:127`, the hint sites at `:185-186` and `:392-393`). A fact names the column and the finding
  ("Quantity is the same for every group in the chart"), never the value. If the local fallback sentence
  needs the value, it is kept in a separate `facts_local` list, which is never passed to `brain_client`.
  `brain_client._compact_caveat_for_transport` additionally drops any `facts` item matching
  `\(\s*=\s*[^)]*\)\s*$` (belt and braces).
  Tests: `test_backstop_facts_carry_no_value`, `test_compact_caveat_drops_value_hint`.
- **`dataset_profile`.** `brain_client._compact_profiles_for_transport` (`brain_client.py:274-323`) drops `min`,
  `max` and `top_values` from EVERY column, not only from `SCHEMA_VALUE_DENY_COLUMNS` columns. It also rewrites
  every constant-column warning to the value-free form `<col> is constant`, already used for deny columns.
  The stored sidecar profile is unchanged; only the transport is scrubbed.
  Tests: `test_profile_transport_has_no_min_max_top_values`, `test_profile_constant_warning_value_free`.
  Brain side (session 2): `_profile_full_block` must render without these keys
  (`test_profile_full_block_without_min_max`).
- **Schema category lists stay.** A text column with ≤ 20 distinct values still lists them in `schema_text`.
  `SCHEMA_VALUE_DENY_COLUMNS` remains the tool for sensitive columns.

#### B7.7 Retry `error_msg` scrub (decision F2 — session 1)

- `brain_client._scrub_error_msg(text: str, keep: frozenset) -> str` runs inside `brain_client.retry` on `error_msg`
  before the POST.
  - Every quoted literal (`'…'` or `"…"`, at most 200 chars inside the quotes) is replaced with `'<value>'`
    unless its content is in `keep`.
  - `keep` = the input column names ∪ the df names, so `KeyError: 'Department'` stays useful for column
    self-correction.
  - The live-SQL class sentences are already value-free and are unaffected.
  - The function never raises; on an exception it returns `"<Type>: <scrubbed>"`, or the exception type only.
- `NearZeroChartError` (`plot_utils.py:2040`) becomes a fixed sentence without `max=` / `range=`. That also removes
  the numbers from the client `EXEC_ERROR` log line (`run_chat_local.py:2120`) and from the brain's `err=` log.
- Tests: `test_scrub_error_msg_replaces_cell_literal`, `test_scrub_error_msg_keeps_column_and_df_names`,
  `test_near_zero_chart_error_has_no_numbers`.

### B8. `pdc_diag.py` (D12)

**Placement.** Client root. A leaf module: the top-level imports are stdlib + numpy + pandas only. plotly is imported lazily inside the plot helpers, sklearn lazily inside `attribute_level(method="forest")`, and shap lazily (not installed; decision F7: keep the fallback, no new dependency).

**Registration:**
- module name `pdc_diag` in the `code_exec` env and `GLOBAL_PLOT_SCOPE` (A3.1);
- the `executor/Dockerfile:67` COPY list;
- `tests/test_executor_structure.py` `RUNTIME_COPY_SOURCES` (`:88-94`);
- the `tests/test_executor_dispatch.py:80` list;
- the web `Dockerfile:72-80` COPY list (together with `narrative.py`), which is pinned by `tests/test_container_hardening_config.py` (it pins every root module).

**"Import whitelist."** None exists: `sandbox_guard.DENIED_MODULES` (`sandbox_guard.py:28-55`) is a denylist, and `pdc_diag` must NOT be added to it. The required change is therefore the three COPY/list entries above.

**Redefinition guard.** Generated code may call `pdc_diag` but never replace it (a BRB retry once wrote its own `detect_anomalies_mock` and erased the real findings). `sandbox_guard.pdc_diag_redefinition(code)` walks the AST before anything runs, at BOTH exec sites (`code_exec._execute_in_process`, `plot_utils._render_in_process`), and refuses:
- the name `pdc_diag` as a target: plain, augmented, annotated, tuple/list unpack, walrus, `for`, `with … as`, `except … as`, comprehension, `del`, `global` / `nonlocal`;
- `class pdc_diag` / `def pdc_diag`, and a `def` / `async def` named after a public function (`PDC_DIAG_PUBLIC`: `fmt`, `coverage_check`, `detect_anomalies`, `decompose_change`, `rate_by_period`, `attribute_level`, `check_metric_overlap`, `plot_highlighted_series`, `plot_waterfall`, `plot_importance`), at any nesting;
- an import that binds `pdc_diag` to something else (`import numpy as pdc_diag`, `from m import x as pdc_diag`, `from m import pdc_diag`); a plain `import pdc_diag` rebinds the real module and is accepted;
- an attribute write or delete on `pdc_diag` (`pdc_diag.fmt = …`, `pdc_diag.x += 1`, `del pdc_diag.fmt`).

A refused block is an ordinary execution error with the fixed text `pdc_diag cannot be redefined` (`PDC_DIAG_REDEFINED_TEXT`; the PYTHON site answers `{"error": …}`, the PLOT site `{"ok": False, "error": …, "trace": ""}`), so the retry loop asks the planner for a new block; the text carries no code and the log line carries the code hash only. An unparsable block is left to `exec`, which reports its own `SyntaxError`. Not caught (defence in depth, not a boundary): `setattr(pdc_diag, …)`, `globals()["pdc_diag"] = …`, writes through `__dict__`. The guard also applies when stored code is re-run (per-item Refresh, dashboard tiles, full-table re-execution, Download Excel): a stored block that defines e.g. `def fmt(…)` now fails there with the same text and the button is frozen. On the local test volume 1 of 161 stored blocks is refused, and it is a BRB retry block that defined `detect_anomalies`.

**Error behaviour.**
- The functions never print or log (they run in the sandbox).
- A missing column or a non-numeric metric raises `ValueError` with a fixed message naming the parameter and the column. That is schema-level information, so it helps retries.
- A missing key of a result, of its `values` or of its `findings` raises a plain `KeyError` whose single argument names the function, the part and the key names pdc_diag set when it built the dict (`_names`, recorded at construction), e.g. `KeyError: "'from_period' is not a key of coverage_check values; available keys: as_of_period, coverage_dim_label, …"` — the key names sorted, VALUES keys with the `prefix=` applied, `none` when there is no key, the missing key's repr capped at 80 characters; key names only, never a value. A key the generated code adds later is never listed: it may be data-derived (`r["values"]["total_" + currency]` would put a category value into the unquoted list, which the retry scrub keeps); a key it deletes later is still listed (it is one of pdc_diag's fixed names). `.get` and `in` are unchanged. Why: BRB run 3 read `from_period`, then `last_period`, then `start_period` from a `coverage_check` result — three guesses, because a bare `KeyError: 'from_period'` names no existing key. The sandbox passes the text through `f"{type(e).__name__}: {e}"` unchanged at both exec sites, and the retry scrub (B7.7) keeps it: the missing key is a string literal of the failed code (a key built at run time becomes `'<value>'`), the outer double-quoted literal holds a single-quoted one, so it is kept whole, and the key list is unquoted.
- Degenerate data (an empty frame, one period) returns empty tables and findings such as `too_few_periods`.

#### B8.1 Constants

```python
PDC_DIAG_VERSION = 1
THIN_PERIOD_RATIO = 0.5                            # a period with rows < 0.5 × median rows is thin (B8.3, B8.4)
SCOPE_ROWS_FACTOR = 3.0                            # an anomaly with > 3× / < 1/3× the previous period's rows is a scope change (B8.4)
FINDINGS_ENUM = (...)                              # B7.1
PDC_DIAG_FINDING_KEYS = frozenset({
  "coverage", "coverage_dim", "thin_period", "period_status", "unknown_dominated", "unknown_dim",
  "units", "metric_gap", "period_order_unknown", "latest_is_current_period", "latest_is_past_period",
  "anomaly", "anomaly_up", "anomaly_is_latest", "multiple_anomalies", "too_few_periods",
  "change_shape", "change_dim", "change_up", "change_large",
  "attribution", "top_feature", "second_feature", "method_fallback",
  "metric_relation", "metrics_identical", "label_disappeared", "rate_up", "rate_down"})
UNKNOWN_TOKENS = frozenset({"", "unknown", "n/a", "na", "none", "null", "nan", "-", "?",
  "не указано", "неизвестно", "неизвестный", "номаълум", "noma'lum", "nomaʼlum", "nomalum", "უცნობი"})
_MONTHS = {...}   # casefolded month names -> 1..12: en full+3-letter; ru nominative+genitive;
                  # uz Latin (yanvar…dekabr); uz Cyrillic (январ…декабр); ka (იანვარი…დეკემბერი)
```

**Return shape** of every analysis function: `{"table": DataFrame, "values": dict, "findings": dict}`. The result, its `values` and its `findings` are `_ResultDict`, a `dict` subclass: `.get`, `in`, `pop`, iteration, `dict(...)`, `json.dumps`, deepcopy and pickle behave as a plain dict; only a missing key differs — it raises a `KeyError` that lists the key names pdc_diag set when it built the dict (Error behaviour above). A pdc_diag result's `values` / `findings` may be handed to the capture as they are (`VALUES = r["values"]`, A3.1).

**Findings conventions:**
- A boolean finding is emitted only when True. The exception is `latest_is_current_period`, which is emitted both ways when computable.
- An enum finding is emitted only when its condition holds.
- A column-valued finding holds the caller's column name.
- No finding ever carries a category value or a number; those go to `values`.

**`prefix`** (default `""`; must match `^([a-z][a-z0-9]{0,9}_)?$`, else `ValueError`) prefixes VALUES keys only. FINDINGS keys are fixed. The error text states the rule in words and quotes nothing ("…then exactly one underscore at the end, for example ovl_…"): the retry scrub (B7.7) turns a quoted literal the failed code does not contain into `'<value>'`, and the earlier `followed by '_'` reached the planner as `followed by '<value>'`, so a prefix such as `granted_vs_new_` was retried unchanged.

#### B8.2 Period ordering — `_order_periods(series, order=None) -> tuple[list, bool]`

The bool means "order known". Rules, in order:
1. `order` given ⇒ its labels that occur in the data, then unseen labels in first-appearance order. The order is known if every occurring label is in `order`.
2. datetime64 dtype ⇒ sorted unique values; known.
3. numeric dtype ⇒ sorted unique values; known.
4. strings ⇒ parse every unique label with `_parse_period_label`, which tries in order:
   - ISO `YYYY-MM[-DD]`, `YYYY/MM`, `MM.YYYY`, `MM/YYYY`;
   - a 4-digit year token plus a `_MONTHS` token (any order, casefolded);
   - `YYYY Qn` / `Qn YYYY` ⇒ the quarter start;
   - `pd.to_datetime(label, errors="coerce")` last.

   If all labels parse, sort by the parsed timestamp (known). Otherwise use first-appearance order (not known), which sets the finding `period_order_unknown: True`.

#### B8.3 `coverage_check`

```python
def coverage_check(df, period_col: str, dims: list, metrics: list, *, order=None,
                   ratio_low: float = THIN_PERIOD_RATIO, ratio_high: float = 2.0, jaccard_min: float = 0.5,
                   share_shift: float = 0.5, vanish_share: float = 0.2,
                   unknown_share: float = 0.5, unit_cols: list | None = None,
                   as_of=None, prefix: str = "") -> dict
```

Let `P` be the ordered periods and `n_p` the rows per period.

**Table:** one row per period: `period, rows, <dim>_members` (distinct non-null per dim), `<metric>_null_share`.

**Findings:**
- `coverage`:
  - `"coverage_change"` when some dim `d` (dims ∪ unit_cols) has a period with `|S_p| < ratio_low·median|S|` or `|S_p| > ratio_high·median|S|`, or when `Jaccard(S_first, S_last) < jaccard_min`, or when, between two consecutive periods that both hold a non-null member of `d`, some member's share of the period's non-null rows moves by more than `share_shift` (absolute; a missing member counts as 0) or a member holding more than `vanish_share` of the earlier period is absent from the later one (a period where `d` is entirely null breaks the chain: the periods on either side of it are not compared). The last rule catches a clean two-value switch (two currencies 50/50, then one), whose Jaccard is exactly 0.5;
  - otherwise `"coverage_stable"`.
- `coverage_dim`: the dim with the lowest Jaccard (ties → argument order), when the change holds.
- `thin_period`: some `n_p < ratio_low · median(n_p)`.
- `period_status = "partial_period"`: the LAST period's `n_last < ratio_low · median(n of the other periods)`.
- `unknown_dominated` + `unknown_dim`: some dim whose share of rows with a null or `UNKNOWN_TOKENS` member (casefolded, stripped) is ≥ `unknown_share` in some period. Among those candidates the dim with the highest share in the LATEST period wins (ties: the higher maximum share, then argument order), and `unknown_dim`, `unknown_dim_label` and `unknown_share_max` (= that dim's highest share over the periods) all name it. Without a candidate, `unknown_share_max` is the highest share of any dim and no label is set. (BRB run 3: SEGMENT peaked at 58.1 % in one month and was reported, while SEKTOR is 100 % Unknown in every 2026 month.)
- `label_disappeared` (`unit_cols` only): a label whose last period with rows is before the last period, while every later period still holds a non-null value of that column — a 0 for it later means no rows, not no activity. With several such labels, the one with the most rows overall (ties: first appearance, unit columns in argument order). BRB: `Ўзбек Сўми (UZS)` ends after 2025 Декабрь; every 2026 row is `Бошқа Валюталар`.
- `units = "mixed_units"`: some unit col has ≥2 distinct non-null members overall.
- `metric_gap`: some metric is entirely null in a period while non-null in another.
- `period_order_unknown`.
- `latest_is_current_period`: the latest parsed period's month equals `pd.Timestamp(as_of or pd.Timestamp.now()).to_period("M")`. Emitted (True or False) only when the periods were parsed.
- `latest_is_past_period`: True only when the periods were parsed AND the latest period's month is before the current month. It is a positive flag, so a template can say "no data for the current month" without a negative section that would also fire on unparsed periods.

**VALUES:** `n_periods, first_period, latest_period, period_from, period_to, latest_rows, median_rows, thin_period_label, coverage_jaccard, unknown_share_max, as_of_period, coverage_dim_label, unknown_dim_label, disappeared_label, disappeared_after_period` (`disappeared_label` is the label text — a category value, a VALUE only, never a finding; `disappeared_after_period` the last period that has it; both None without the finding; labels as `str`; the two `*_dim_label` values are the caller's column names, so templates can name the dimension; `period_from` / `period_to` are aliases of `first_period` / `latest_period` — the names the planner's period-scope rule tells the code to set for the headline).

#### B8.4 `detect_anomalies`

```python
def detect_anomalies(df, period_col, metric, agg: str = "sum", *, order=None,
                     z: float = 3.5, ratio: float = 2.0, thin_ratio: float = THIN_PERIOD_RATIO,
                     rows_col=None, dims=None, prefix: str = "") -> dict
```

- `agg ∈ {"sum", "mean", "median", "count", "nunique"}`; `v_p` = the aggregate per period; `n_p` = the rows per period.
- `len(P) < 3` ⇒ `too_few_periods: True` and no `anomaly`.
- `m = median(v)`, `MAD = median(|v - m|)` — over ALL periods, data gaps included.
- Robust z = `0.6745·(v_p - m)/MAD`. If MAD = 0, use the mean absolute deviation instead; if that is also 0, z = 0.
- `ratio_med_p = v_p/m` (NaN unless m > 0).
- `ratio_prev_p = v_p/v_{p-1}` (NaN unless `v_{p-1} > 0`).
- **Data gap.** A period is a gap when `n_p < thin_ratio · median(n)` (the `coverage_check` thin rule) or when `v_p == 0` while every existing neighbour is finite and non-zero, or (the COLLAPSE rule, applied ONLY to pre-aggregated input — every period holds at most one row, `max(n) ≤ 1`; on raw rows the row-count rule, with the zero rule, alone decides, so a real value collapse over normal row counts stays an anomaly) when `v_p ≥ 0` is below `COLLAPSE_RATIO` (0.01) × every existing neighbour, each finite and > 0 (BRB run 2, English q3: February 4,000,000 against 563.70 bn and 4,739.29 bn, passed as one row per month). A gap is a coverage problem, not a business anomaly: it is never flagged and never primary. (BRB: February 2026 has 3,792 rows against a median of 12,617 and NPL_COUNT 1; it used to be the primary anomaly, read as a "sharp increase".)
- A non-gap period `p` is flagged iff `|z_p| ≥ z` OR `max(ratio_med_p, 1/ratio_med_p) ≥ ratio`.
- Primary = the flagged period with the largest `|log(ratio_med_p)|`; ties go to the later period.
- **Row counts.** `n_p` is the period's row count, or — with `rows_col` (a numeric column; missing / non-numeric ⇒ `ValueError` naming `rows_col`) — the period's sum of it, for the table's `rows` and the gap rules alike. Rows are KNOWN with `rows_col` or when some period holds more than one row (raw rows), and only while the rows sum above 0 (an all-zero / all-NaN `rows_col` leaves them unknown). NaN cells and a non-finite sum count as 0 rows. With `rows_col` the COLLAPSE rule is off (the counts are known; the thin-rows and zero rules decide).
- **Scope change.** The primary `p` (when `p > 0`) is compared with the period right before it, `p-1`, data gap or not: with known rows, `n_p / n_{p-1} > SCOPE_ROWS_FACTOR` or `< 1 / SCOPE_ROWS_FACTOR` (an `n_{p-1}` of 0 counts as past it only when `n_p > 0`); or, with `dims`, a dim whose member sets of the two periods have Jaccard < 0.5 or whose member row shares shift (the B8.3 share rule, 0.5 / 0.2). Either ⇒ the period's totals reflect a change of reporting SCOPE, not a business change: `anomaly = "scope_change"` instead of `"anomaly_confirmed"`, and neither `anomaly_up` nor `anomaly_is_latest` is set (no spike section opens); the anomaly VALUES stay. BRB run 3: March holds 33,455 rows against February's 3,792 (8.82×) and about 10× January's portfolio, while the NPL rate fell (8.69 % → 7.53 %); both q3 answers had called it "the primary problem". (Measured against the previous NON-gap period, January, the ratio is 2.96 and would not fire; decided 2026-10-25: the period right before.)

**Findings:**
- `anomaly` (`"anomaly_confirmed"` iff at least one non-gap period is flagged and the primary is not a scope change, `"scope_change"` when it is, else `"no_anomaly"`);
- `anomaly_up`, `anomaly_is_latest`, `multiple_anomalies` — over non-gap flagged periods only; `too_few_periods`;
- `period_status = "data_gap"` when some period is a gap (the existing key and code: a template writes `{{#data_gap}}…{{/data_gap}}`). `coverage_check` uses the same key for `"partial_period"`; in a block that copies both results into FINDINGS the later copy wins, and the thin month is still reported by `coverage_check`'s `thin_period`.

**VALUES:** `anomaly_period, anomaly_value, baseline_value (=m), anomaly_ratio_median, anomaly_prev_period, anomaly_prev_value, anomaly_ratio_prev, n_periods, n_anomalies, data_gap_period, period_from, period_to, rows_ratio` (`rows_ratio` = `n_p / n_{p-1}` for the primary whenever the rows are known and `n_{p-1} > 0`, scope change or not, else None; `n_anomalies` counts non-gap flagged periods; `data_gap_period` is the label of the gap with the fewest rows, then the lowest value, then the earliest, None without a gap; `period_from` / `period_to` are the first / last period label, None without periods).

**Table:** `period, value, robust_z, ratio_prev, ratio_median, is_anomaly, is_primary, rows, is_data_gap` (`rows` and `is_data_gap` are additive trailing columns).

#### B8.5 `decompose_change`

```python
def decompose_change(df, period_col, metric, from_period=None, to_period=None, dims=(), *,
                     agg: str = "sum", order=None, top_k: int = 5, concentrated_share: float = 0.6,
                     even_tolerance: float = 0.15, large_ratio: float = 2.0, unknown_share: float = 0.5,
                     rows_col=None, prefix: str = "") -> dict
```

**Defaults.** `to_period=None` ⇒ the latest period. `from_period=None` ⇒ the period before `to_period`. Fewer than 2 periods ⇒ `too_few_periods`.

**Unknown-dominated dims.** A dim whose share of rows with a null or `UNKNOWN_TOKENS` member is above `unknown_share` in the from period OR the to period cannot explain the change. It is skipped: no table rows, never `change_dim`, never the source of `change_top_member`, and it takes no part in the `change_shape` decision. The first such dim (argument order) is reported as `unknown_dominated: True` + `unknown_dim`. (BRB: SEKTOR is 100 % `Номаълум` in January and February, so it once produced `change_top_member = Номаълум` with share 1.0.)

**Per dim** (members of both periods; a missing member counts as 0):
- `from_v`, `to_v`, `delta = to_v - from_v`;
- `share_of_delta = delta / Σdelta` (defined when Σdelta ≠ 0);
- `share_of_base = from_v / Σfrom_v` (defined when Σfrom > 0);
- `mismatch = |share_of_delta - share_of_base|`.

A dim is *concentrated* iff its top-`|delta|` member has `share_of_delta ≥ concentrated_share` AND `mismatch > even_tolerance`. A dim is *even* iff `max mismatch ≤ even_tolerance` over members with `share_of_base ≥ 0.01` or `share_of_delta ≥ 0.01`.

**Findings:**
- `change_shape`:
  - `"concentrated_in_one_dim"` if any dim is concentrated, with `change_dim` = the dim with the largest top share;
  - otherwise `"spread_evenly"` if every dim is even;
  - `"scope_change"` when `to_period="primary_anomaly"` lands on a scope change (below);
  - otherwise absent.
- `change_up` = Σdelta > 0.
- `change_large` = `to/from ≥ large_ratio` or `to/from ≤ 1/large_ratio`.

**VALUES:** `change_from_period, change_to_period, change_from_value, change_to_value, change_abs, change_ratio, change_pct (fraction), change_top_member, change_top_member_share, change_top_member_delta, change_dim_label` (`change_dim_label` = the column name of the dim the top member comes from, as `str`: `change_dim` when the change is concentrated, otherwise the usable dim with the largest top share). `change_dim_label` is set exactly when `change_top_member` is; with no usable dim all four `change_top_member*` / `change_dim_label` values are None.

**`to_period="primary_anomaly"`.** `decompose_change` runs `detect_anomalies(df, period_col, metric, agg=agg, order=order)` internally and uses its primary anomaly period as `to_period`; the period before it is the default `from_period`. If no anomaly is found, the latest period is used and `anomaly` is set to `"no_anomaly"` in findings. The internal call gets `rows_col` (never `dims`, so a decomposition dim cannot turn an anomaly into a scope change). When it reports `"scope_change"`, that primary period is the to period, the totals and `change_*` VALUES are computed as usual (`change_up` / `change_large` too), but NO member is attributed: `change_shape = "scope_change"`, `anomaly = "scope_change"`, an empty table (attrs `pdc_metric`, `pdc_from_period`, `pdc_to_period`, no `pdc_change_dim`), `change_top_member*` / `change_dim_label` None, no `change_dim` and no `unknown_dominated`. (BRB run 3 en q3 credited Юридик with 73.9 % of a change measured from the data-gap month.) This lets phase 2 target the anomaly without seeing phase-1 values.

**Table:** `dim, member, from_value, to_value, delta, share_of_delta, share_of_base`. It holds the top_k members per dim by `|delta|`, plus one `"(other)"` row per dim. `table.attrs` carries `pdc_metric`, `pdc_from_period` / `pdc_to_period` and, whenever a top member is set, `pdc_change_dim` = the dim `change_dim_label` names.

#### B8.6 `attribute_level`

```python
def attribute_level(df, target, dims, numeric_cols, *, method: str = "auto",
                    dominant_ratio: float = 2.0, weak_share: float = 0.05,
                    weight: str | None = None, max_rows: int = 200_000, prefix: str = "") -> dict
```

- Drop null targets and sample down to `max_rows` (`random_state=0`).
- `method="auto"` / `"variance"`:
  - dim importance = weighted eta² `SS_between/SS_total`, with members under 0.5% of rows pooled as `"(rare)"`;
  - numeric importance = Spearman ρ².
- `method="forest"`: a lazy `sklearn.ensemble.RandomForestRegressor(n_estimators=100, max_depth=8, random_state=0, oob_score=True)` on one-hot dims (top 20 members each) plus the numerics. Importance per original column = Σ impurity importance × `max(oob R², 0)`.
- `method="shap"`: a lazy `shap` import; an ImportError falls back to forest.
- On any ImportError or exception the chain `shap → forest → variance` falls back, and a fallback sets `method_fallback: True`.

**Findings:**
- `attribution`:
  - `"dominant_feature"` iff `top ≥ weak_share` and (`second == 0` or `top/second ≥ dominant_ratio`);
  - `"weak_signal"` iff `top < weak_share`.
- `top_feature` (column).
- `second_feature` (column, when it is ≥ `weak_share`).
- `method_fallback`.

**VALUES:** `top_feature_label, second_feature_label, top_feature_share, second_feature_share, n_rows_used, top_member` (for a dim: the member with the largest `|mean − overall|`), `top_member_target, overall_target`.

**Table:** `feature, kind ("dimension"|"numeric"), importance, top_member, top_member_target`.

#### B8.7 `check_metric_overlap`

```python
def check_metric_overlap(df, metric_a, metric_b, *, equal_share: float = 0.5,
                         rel_tol: float = 1e-9, min_compared_share: float = 0.1,
                         prefix: str = "") -> dict
```

- Compares the rows where both metrics are non-null AND not both zero (two sparse metrics that are zero in most rows are not the same metric). When no row, or fewer than `min_compared_share` of the non-null rows, is compared, the equal share is `None` and no finding is emitted. Two values are equal iff `|a−b| ≤ rel_tol·max(|a|, |b|, 1)`.
- **Findings:**
  - `metric_relation = "metric_definition_overlap"` iff the equal share ≥ `equal_share`;
  - `metrics_identical` iff the equal share ≥ 0.99.
- **VALUES:** `overlap_equal_share` (fraction of the compared rows), `overlap_sum_ratio` (Σa/Σb over the non-null rows), `overlap_rows` (the compared rows), `overlap_nonnull_rows`.
- **Table:** one summary row.

#### B8.8 Chart helpers

Plotly, one figure each, English text, `template="plotly_white"`, with this palette:

```python
_PALETTE = {"base": "#4C78A8", "highlight": "#E45756", "up": "#54A24B", "down": "#E45756", "total": "#4C78A8"}
def plot_highlighted_series(table, period_col="period", value_col="value", *, highlight_col="is_primary",
                            title=None, y_title=None) -> "go.Figure"   # bar per period, highlighted bar in highlight colour
def plot_waterfall(table, *, dim=None, title=None, y_title=None) -> "go.Figure"   # from decompose_change table:
    # start = Σfrom, one bar per member delta (top_k + "(other)") of `dim` (default: first dim), end = Σto
def plot_importance(table, *, title=None, x_title="Share of variation explained") -> "go.Figure"  # horizontal bars, sorted
```

- The caller writes `fig = pdc_diag.plot_…(r["table"], …)`.
- A table that lacks the required columns ⇒ `ValueError`.
- Default titles: `"<value_col> by period — highlighted period"`, `"What changed: <metric> by <dim>, <from> to <to>"`, `"What explains <target>"`.
- `plot_waterfall` plots `table.attrs["pdc_change_dim"]` when present and in the table, even when another `dim=` is passed, so the chart always shows the dimension the findings and the text name; a table without it (built by hand) uses `dim`, else its first dim. The title (given or default) gains `" (<dim>)"` unless it already names the dim (case-insensitive).

#### B8.9 `rate_by_period`

```python
def rate_by_period(df, period_col, numerator, denominator, *, order=None, prefix: str = "") -> dict
```

The rate view to compare when totals move with the row count (`scope_change`): numerator sum / denominator sum per period (BRB: NPL_AMOUNT / LOAN_AMOUNT).
- Periods ordered as B8.2; `numerator` / `denominator` must exist and be numeric (`ValueError` naming the parameter).
- **Table:** `period, numerator, denominator, rate` (the per-period sums; `rate` NaN when the denominator is 0 or not finite); attrs `pdc_numerator`, `pdc_denominator`.
- **VALUES:** `rate_first`, `rate_last` (fractions — the `:pct` kind — of the first and the last period with a finite rate), `rate_change_pp` = (last − first) × 100 (percentage points), `rate_first_period`, `rate_last_period`, `period_from`, `period_to` (the first and last periods), `n_periods`; None when not computable.
- **Findings:** `rate_up` when `rate_change_pp > 0.5`, `rate_down` when `< -0.5`; fewer than two finite rates ⇒ `too_few_periods` (no change, no up / down).
- First vs last period, by decision (2026-10-25): on BRB that is 17.96 % (2025 Ноябрь) → 7.53 % (2026 Март), `rate_down`; the January → March view (8.69 % → 7.53 %) is read from the table.

### B9. Two-phase flow (D8)

#### B9.1 State machine (`run_chat_multi_plot`)

```
P1_RUN ──(all phase-1 blocks done)──► P1_NARRATIVE ──► DECIDE
DECIDE:
  not plan.diagnostic                         → DONE (no log)
  merged phase-1 FINDINGS.needs_phase2 is not True → DONE
  phase-1 ok_blocks == 0                     → DONE, PHASE2_SKIPPED reason=phase1_failed
  service_down_text set                      → DONE, PHASE2_SKIPPED reason=service_down
  monotonic() - t_turn_start ≥ PHASE2_START_MAX_ELAPSED_S (120) → DONE, PHASE2_SKIPPED reason=turn_time
  else → EMIT_P1_TABLES → EMIT_P2_PROGRESS → P2_PLAN
P2_PLAN: deadline = monotonic() + PHASE2_BUDGET_S (90)
  plan error (BrainError incl. timeout, raised)      → P2_FAILED(plan_error)
  kind not in (PYTHON, PLOT_CODE)                    → P2_FAILED(text_kind)
  blocks = _extract_multi_plot_blocks(raw_text) or [plan.code]; empty → P2_FAILED(no_code)
P2_RUN: per block — budget check before render/execute and before each retry; max 1 retry per block
  budget exceeded                                     → P2_FAILED(budget)
  all phase-2 blocks failed                           → P2_FAILED(exec_error)
P2_NARRATIVE (ok_blocks ≥ 1) → yield narrative phase 2 → DONE (phase2_status="done", PHASE2_DONE blocks= elapsed_ms=)
P2_FAILED(reason) → log PHASE2_FAILED reason=… → phase-2 narrative dropped, streamed phase-2 visuals kept → DONE
Stop (user): the worker breaks at the next event boundary (routes/chat.py:1746); the generator is never resumed.
```

- Single flow: `plan.diagnostic` with `needs_phase2` is ignored and logged as `PHASE2_SKIPPED reason=single_flow`.
- `t_turn_start` is taken immediately before the phase-1 `brain_client.plan` call (`run_chat_local.py:1578`).
**Real time limits.** The 330 s stream timeout in the browser (`static/dashboard.js:3178`) is cleared at the first partial or done (`:3290`, `:3318`), so it does not bound a streamed phase 2. Two limits do:
- the reload/recover poll gives up at `MAX_MS = 330000` (`dashboard.js:1848`);
- edit-regenerate is ONE non-streaming POST with a 330 s wall (`dashboard.js:3584-3598`).

What the spec does about them:
- **Edit-regenerate never runs phase 2.** It logs `PHASE2_SKIPPED reason=non_streaming`; the generator is called with `allow_phase2=False`.
- **The 90 s budget is enforced per call, not only between calls:**
  - the phase-2 `brain_client.plan` gets `timeout=min(60, remaining)` (`_post` already takes a per-call timeout);
  - every phase-2 executor call gets `timeout_s=min(<normal timeout>, remaining)`;
  - an executor queue wait counts against `remaining`.
- **Start gate.** The 120 s `PHASE2_START_MAX_ELAPSED_S` start gate keeps a normal turn inside the reload poll window.

#### B9.2 Phase-2 request

```python
brain_client.plan(sid, question, schema_text=schema_str, df_names=df_names,
                  history_rows=history_rows + [p1_row],   # the SAME pre-turn rows as phase 1 + one row (F4)
                  common_fields=common_fields, user_email=user_email,
                  dataset_profile=dataset_profile, live_tables=live_tables,
                  capabilities=caps, findings=<merged validated phase-1 FINDINGS>)
```

`p1_row` = `{"role": "ai", "content": "[Phase 1 of this answer — code only, no values]", "code": <phase-1 block codes joined with "\n\n###NEXT_PLOT###\n\n">}` (decision F4). It is code only; the marker/template are not repeated, and `findings` carries the codes. On the brain side, when `findings` is present the LAST history row is rendered with `code[: PROMPT_HISTORY_TRIM_CHARS * 3]` (the include_previous_code length), so the planner sees what phase 1 computed (session 3).

The phase-2 live state reuses the turn's `state`: `_new_live_state` is NOT rebuilt, and `_merge_retry_sql(state, p2_plan)` merges the new plan in.

#### B9.3 Phase-2 reply handling

- The reply has its own blocks and its own `###NEXT_PLOT###` split. The caption index is counted within the phase-2 reply.
- It gets its own `_NarrState(phase=2)`.
- `needs_phase2` in phase-2 FINDINGS is ignored.
- `chart_n` continues from phase 1.
- Phase-2 table blocks ride the done event, with `table_phases` = 2.

#### B9.4 Worklist refactor (exact names)

The loop body `run_chat_local.py:1679-1954` and the table loop `:1963-2023` move into:

```python
def _run_blocks(*, ctx: "_TurnCtx", narr: _NarrState, blocks: list[tuple[int, str]],
                max_retries: int, deadline: float | None):   # generator of partial events
```

- `_TurnCtx` holds `sid, dfs, schema_docs, schema_str, df_columns (the existing {name: [cols]} dict), input_columns (A5.1), df_names, question, history_rows, user_email, dataset_profile, live_tables, state, caps, total_usage, produced, produced_sigs, produced_data_sigs, service_down_text, last_live_failure`.
- Worklist items become `(code, ma_retry, np_retry, mx_retry, block_index)`.
- Phase 1 calls it with `max_retries=3, deadline=None`; phase 2 with `max_retries=1, deadline=<deadline>`.
- A deadline breach raises an internal `_Phase2Budget`, which the orchestrator catches.

#### B9.5 Storage

- Phase 1 goes in the top-level fields and phase 2 in `phase2`. Each image carries `images[i].phase`; tables carry `table_phases`.
- Record `code` = all phase-1 and phase-2 block codes joined with `"\n\n###NEXT_PLOT###\n\n"` (`routes/chat.py:557`), so `code_is_stored` covers phase-2 visuals.
- `content` = `marker(f1) + "\n" + marker(f2) + "\n" + t1 + "\n\n" + t2`. An empty marker contributes no line. Both markers come first so that the brain's 800-character history trim keeps them.
- **Phase-2 rendering inputs.** Phase 2 renders with `values = v1 | v2` and `findings = f1 | f2`, phase 2 winning on a key collision. The phase-2 marker and `phase2.findings` hold f2 only.
- `phase2_status`.

#### B9.6 UI

See A5.6. Phase-2 visuals that already streamed stay visible on failure, and the phase-2 narrative is dropped (decision F5).

### B10. Brain contract (D1, D9, D10, D11)

#### B10.1 Capabilities

```python
def _clean_capabilities(value) -> frozenset:
    """list of str -> known subset; diagnostics_v1 kept only with narrative_v1; two_phase_v1 kept only with
    diagnostics_v1. Never raises."""
```

**Plumbing and logs:**
- `routes/llm.py` `/v1/plan` (`:88-157`) and `/v1/retry` (`:160-220`) read `body.get("capabilities")` and pass it on raw.
- `plan_code(..., capabilities=None, findings=None)` and `retry_code(..., capabilities=None)` gain keyword parameters, appended last.
- `BRAIN_PLAN` / `BRAIN_RETRY` logs add `caps=",".join(sorted(_clean_capabilities(raw))) or None` and `findings_n=len(_clean_findings(raw)) or None`. Both are computed from the CLEANED values, never from the raw request.
- `_findings_prompt_block` is placed AFTER the common-fields section, so the `CODE_PROMPT` log snippet (`brain_agent.py:1589`, the first 800 characters of the user prompt) normally does not contain it. Even when it does, the block holds only codes and input column names.
- `BRAIN_PLAN_DONE` adds `narrative=<bool>`, `captions_n` and `diagnostic=<bool or None>`.

**Byte-identical rule.** Empty `caps` (absent, None, `[]`, a non-list, or only unknown names) ⇒ the prompt, system instruction, classifier prompt and response are identical to today. Pinned by `test_plan_without_capabilities_is_byte_identical` and `test_retry_without_capabilities_is_byte_identical`.

**Classifier threading.**

```python
# empty caps:  select_model_for_query(question, history_messages, session_id=sid)                 (unchanged call)
# diagnostics: select_model_for_query(question, history_messages, session_id=sid, capabilities=caps)
def select_model_for_query(question, history_messages=None, session_id="unknown", capabilities=None)
def _detect_query_complexity(question, history_messages=None, session_id="unknown", capabilities=None)
```

This keeps the existing test stubs `fake_select(question, history_messages, session_id=...)` working.

**Prompt assembly order.**

Planner system instruction (`brain_agent.py:1548-1585`):

```
planner_system
+ "\n\n" + PROMPTS["planner_narrative_system"]                      (narrative_v1)
+ tenant prompt_tuning_planner
+ skill_ctx (skills_needed with "diagnostics" removed)
+ "\n\n=== DIAGNOSTICS GUIDANCE ===\n" + diagnostics.yaml           (diagnostic gate)
+ domain vocabulary
```

Planner user prompt (`:1494-1546`):

```
… schema … profile …
+ _findings_prompt_block(findings)                                   (non-empty cleaned findings)
+ common fields
+ planner_human_suffix
+ "\n\n" + planner_narrative_suffix                                  (narrative_v1)
+ live block
```

Retry system instruction:

```
_build_retry_system_instruction(...)
+ live retry block
+ "\n\n" + PROMPTS["retry_narrative_system"]                         (narrative_v1)
```

**Diagnostic gate.**

```python
diag_gate = ("diagnostics_v1" in caps and
             (task_type in {"diagnostic", "advisory"} or "diagnostics" in raw_skills_needed))
```

`raw_skills_needed` always has `"diagnostics"` removed before `build_skill_context_from_list` / `build_skill_context`. The YAML is loaded with `skills.skill_loader._load_yaml_file(str(CORE_DIR / "diagnostics.yaml"))` and logged as `DIAGNOSTICS_SKILL_LOADED`.

**`diagnostic: true`** is set only when all of these hold:
- `"two_phase_v1" in caps` (sessions 1+2 never send it, so no plan carries the flag until session 3 ships);
- `"diagnostics_v1" in caps`;
- task_type ∈ {diagnostic, advisory};
- `result["kind"] in _CODE_KINDS`;
- the cleaned request `findings` is empty.

It is never sent as `false`, and never on `/v1/retry`.

**Related changes:**
- `_PLOT_CODE_PATTERNS` (`brain_agent.py:515-520`) gains `"pdc_diag.plot_"`. A one-block reply that draws through pdc_diag is then classified PLOT_CODE; otherwise `safe_execute` would capture only matplotlib (`code_exec.py:325`) and the plotly figure would be lost. The change is byte-safe for old clients, whose prompts never mention pdc_diag. Test: `test_pdc_diag_plot_is_plot_code`.
- `_DATA_TASK_TYPES` (`brain_agent.py:1313`) = `{"aggregation", "visualization", "statistical_analysis", "complex_analysis", "diagnostic", "advisory"}`.
- `_fallback_complexity` (`:1023-1036`) is unchanged: no Uzbek keywords are added, task_type stays `"unknown"`, and diagnostic is never inferred from keywords.
- `_clean_findings(value) -> dict` is shared by `/v1/plan` and describe. It accepts a dict of ≤ 24 keys matching `^[a-z][a-z0-9_]{0,39}$`, each value a `bool` or a `str` of 1–64 chars without `[\x00-\x1f\x7f]`.

#### B10.2 Classifier section (diagnostics_v1 only)

Changes inside the prompt at `brain_agent.py:1082-1167`. Without `diagnostics_v1`, every byte is unchanged.
1. The task_type enum in the JSON template line (`:1112`) becomes `<greeting|data_retrieval|aggregation|visualization|statistical_analysis|complex_analysis|explanation|diagnostic|advisory>`.
2. `AVAILABLE SKILL MODULES` (`:1106-1109`) gets a new line `  diagnostics` after `analytics_output, analytics_dashboard`.
3. The libraries list (`:1094-1099`) gets a new line: `- pdc_diag — deterministic diagnostic helpers (coverage, anomalies, change decomposition, attribution)`.
4. The following text is inserted after the explanation definition paragraph (`:1116-1120`) and before `Scoring guide:`:

```
task_type "diagnostic": the user asks WHY something changed or looks the way it does, WHAT DRIVES or
explains a metric, WHAT PROBLEMS or anomalies the data shows, or for a root cause — and answering needs
new computation on the data. Examples:
- Uzbek: "Mart oyida kreditlar soni nima uchun keskin oshdi?" / "Bu ma'lumotlarda qanday muammolar bor?" / "Muddati o'tgan kreditlar ulushi nimaga bog'liq?"
- Russian: "Почему в марте выросла просрочка?" / "Какие проблемы видны в этих данных?"
- Georgian: "რატომ შემცირდა გაყიდვები თებერვალში?" / "რა პრობლემებია ამ მონაცემებში?"
- English: "Why did revenue drop in the third quarter?" / "What drives the default rate?"
task_type "advisory": the user asks WHAT TO DO, what to recommend or how to improve, grounded in the
loaded data (not general advice). Examples:
- Uzbek: "Kredit portfelini yaxshilash uchun nima qilishimiz kerak?" / "Qaysi hududlarga e'tibor qaratishni tavsiya qilasiz?"
- Russian: "Что нам сделать, чтобы снизить просрочку?" / "Какие сводки нам стоит регулярно делать по этой базе?"
- Georgian: "რა უნდა გავაკეთოთ ვადაგადაცილების შესამცირებლად?" / "რომელ რეგიონებზე გვირჩევთ ყურადღების გამახვილებას?"
- English: "What should we do to reduce churn?" / "Which segments do you recommend we focus on?"
Precedence: a why / what-drives / what-problems question is "diagnostic" even when it also asks for a
dashboard, an overview or several charts (diagnostic wins over complex_analysis); a what-to-do question is
"advisory"; a dashboard or overview request with neither stays "complex_analysis". A question about a
PREVIOUS result answerable from the conversation without new computation stays "explanation".
For diagnostic and advisory: complexity_score AT LEAST 8, skills_needed includes "diagnostics" and
"analytics_viz", and suggested_approach MUST be: "Follow the diagnostic playbook: coverage check, confirm
the anomaly, then decompose the change or attribute the level; ###NEXT_PLOT### blocks with VALUES and
FINDINGS; one narrative template."
```

The Georgian, Russian and Uzbek examples need a native-speaker check before shipping.

#### B10.3 `prompts.json["planner_narrative_system"]` (exact text)

The following text is the prompt verbatim.

````text
NARRATIVE ANSWER TEXT — the client writes the numbers, you write the words.
You never see result values. For every data answer (a ```python``` or ```plot_code``` reply, single or ###NEXT_PLOT### multi-block) you write the answer text as a TEMPLATE. The client fills it after your code runs. End the reply with exactly ONE ```narrative block, placed AFTER the last code block (never between code blocks):
```narrative
<template>
```

TEMPLATE LANGUAGE
- Write the template in the answer language defined at the top of the prompt. Plain markdown only: **bold**, lines starting with "- ", blank lines. No HTML, no headings, no code, no three backticks inside the template. At most 4000 characters.
- A value slot is {{name}} or {{name:kind}}. name: letters, digits and underscores, starting with a letter or underscore, at most 40 characters, no spaces inside the braces. kind is one of: count (whole number), float (two decimals), pct (the stored value is a FRACTION: 0.125 is shown as 12.5%), ratio (shown as 3.4x), amount (large money or volume, shown as 1.25 bln / 7.25 mln), text (a label such as a category name), period (a period label).
- Conditional text: {{#flag}}text{{/flag}} is shown only when FINDINGS[flag] is True, or FINDINGS holds a column name or code under flag, or flag is a finding code (for example spread_evenly) that some FINDINGS value equals. {{^flag}}text{{/flag}} is shown only when that is not the case. One level only: never put a section inside a section. Slots may be used inside sections. Put a whole bullet line inside its section ({{#x}}- …{{/x}}) so a hidden bullet leaves no empty line.
- NEVER write a number, total, percentage, date, name or category value yourself — not as an estimate, not as an example. Every value comes from a slot. Only numbers that are part of the question itself may be repeated.
- The FIRST sentence directly answers the question and contains its slot(s). The LAST sentence says how to read the chart or table (what the axes show, which bar is highlighted). Two to six sentences unless the user asked for a list.

VALUES — the source of every slot
- In the code assign the numbers and labels the template uses to the dict VALUES, for example VALUES["total_loans"] = int(total); VALUES["latest_period"] = str(latest). VALUES and FINDINGS already exist as empty dicts in every block; use VALUES["key"] = … or VALUES.update(...). Never print them.
- Allowed VALUES: int, float, bool, str (at most 200 characters) or None. Convert numpy numbers with int() or float(), dates with str() or .strftime(). A DataFrame, Series, list or Timestamp is dropped. At most 40 keys per reply.
- Every slot must be set by some block's VALUES; a slot nobody sets is shown as "n/a". For a pct slot store a share (part / whole), never a number already multiplied by 100.
- Keep RESULT and fig exactly as before; VALUES is additional, never a replacement. Every block must still produce a figure (fig) or a RESULT table — a block that only computes VALUES is not allowed.

FINDINGS — codes, never values
- FINDINGS records checks your code computed, for example FINDINGS["has_gaps"] = bool(gaps). Allowed values: True or False; an exact column name of the loaded data; or one of these codes: coverage_change, coverage_stable, partial_period, metric_definition_overlap, mixed_units, concentrated_in_one_dim, spread_evenly, weak_signal, dominant_feature, anomaly_confirmed, no_anomaly, data_gap, outlier_driven, seasonal_pattern. Anything else is dropped.
- Keys: lowercase letters, digits and underscores, at most 40 characters, written in the code as a quoted string literal — never built from data (no f-strings or concatenation for keys). At most 24 keys per pass, pdc_diag's included.
- FINDINGS select the template's conditional text and are the ONLY result information you will see in later turns.

CAPTIONS (optional)
- After the ```narrative block you may add one ```caption:<n> block per visual: n is the position of the code block in THIS reply, counting every ```python``` and ```plot_code``` block from 1 as split by ###NEXT_PLOT###; 1 ≤ n ≤ 12. Same template language, at most 600 characters, one or two sentences, using only VALUES set by block n itself. A visual without a caption gets a short generic description instead.

HELPER LIBRARY pdc_diag (pre-imported in every block; do not import it)
- pdc_diag.coverage_check(df, period_col, dims, metrics, unit_cols=[...]), pdc_diag.detect_anomalies(df, period_col, metric, agg="sum"), pdc_diag.decompose_change(df, period_col, metric, from_period=None, to_period=None, dims=[...]), pdc_diag.attribute_level(df, target, dims, numeric_cols), pdc_diag.check_metric_overlap(df, metric_a, metric_b); charts: fig = pdc_diag.plot_highlighted_series(r["table"]), fig = pdc_diag.plot_waterfall(r["table"]), fig = pdc_diag.plot_importance(r["table"]).
- A block that calls pdc_diag.plot_* (or otherwise builds fig) MUST be a ```plot_code block, never ```python.
- Each analysis function returns {"table": DataFrame, "values": dict, "findings": dict}: use VALUES.update(r["values"]) and FINDINGS.update(r["findings"]). Their finding keys are fixed and allowed without being written in your code; pass prefix="xyz_" to keep VALUES keys of two calls apart. Periods are ordered chronologically, month names in English, Russian, Uzbek and Georgian included; pass order=[...] when labels are not dates.

SANITY HOOKS (every data answer)
- A total over several periods: when a column looks like a unit or currency, call pdc_diag.coverage_check with unit_cols=[that column] and branch the template on {{#mixed_units}}…{{/mixed_units}} and {{#coverage_change}}…{{/coverage_change}}.
- Two metric columns that may measure the same thing (for example a "granted" count and an "active" or "portfolio" count): call pdc_diag.check_metric_overlap and branch on {{#metric_definition_overlap}}…{{/metric_definition_overlap}}.
- A stock (balance, outstanding, active, portfolio) is never summed across periods: report the latest period or each period. The one exception is an explicit request for a total over time (see PERIOD SCOPE AND STOCK METRICS).
- "This month", "current period", "now": compute the latest period in the data; coverage_check sets latest_is_past_period when the data ends before the current month; then the template says, inside {{#latest_is_past_period}}…{{/latest_is_past_period}}, that the latest period in the data is {{latest_period:period}}.

CHECKS, FINDINGS AND SLOTS (every data answer)
- Checks before the answer: a "how many" or "what amount" answer that aggregates over a period column calls, in its FIRST block and BEFORE the answer is computed, pdc_diag.coverage_check(df, <period col>, dims=[<categorical columns>], metrics=[<the metric>], unit_cols=[<currency / unit columns>]) and, when the data has a second column of the same kind that the metric may duplicate (a granted / issued column against an active / portfolio / total column), pdc_diag.check_metric_overlap(df, <the metric>, <that column>); both results are merged with VALUES.update(r["values"]) and FINDINGS.update(r["findings"]). The same block then computes the answer from those results: per currency or unit label when r["findings"] reports mixed_units, and for the latest complete period when it reports metric_definition_overlap (unless the user explicitly asks for a total over time, see PERIOD SCOPE AND STOCK METRICS). A block never sees another block's results, so a later block that needs them calls them itself. After the first sentence, the narrative carries the sentence of every check that ran, each in its own section, in the answer language and otherwise unchanged: {{#coverage_change}}Note: the set of {{coverage_dim_label:text}} values changes between periods, so totals are not comparable.{{/coverage_change}} {{#mixed_units}}Note: the amounts carry more than one currency or unit label, so they are reported per label and never added together.{{/mixed_units}} {{#metric_definition_overlap}}Note: in {{overlap_equal_share:pct}} of rows the two metrics are equal, so the one asked about counts a stock (items still active), not new items, and adding periods together counts the same items more than once.{{/metric_definition_overlap}}
- No home-made flags: where a pdc_diag function provides a finding or value — coverage (coverage_check), units (coverage_check with unit_cols), overlap (check_metric_overlap), anomaly (detect_anomalies), change decomposition (decompose_change), attribution (attribute_level) — the code uses that result and never computes its own version of it: no own currency or unit flag (for example FINDINGS["has_other_currencies"] = len(by_currency) > 1), no own threshold, mean-plus-k-sigma or outlier rule, no own change ratio or top member, no own unknown share. Code computes additional VALUES and FINDINGS only for things pdc_diag does not provide (the answer figure, its period scope, a share or rank the question asks for).
- Slot safety: a slot whose value comes from a finding is used only inside a section of that finding, never in an unconditional sentence or conclusion, so a finding that did not fire never renders "n/a": {{coverage_dim_label:text}} inside {{#coverage_change}}; {{unknown_dim_label:text}} inside {{#unknown_dominated}}; {{thin_period_label:period}} inside {{#thin_period}}; {{change_dim_label:text}} and {{change_top_member:text}} inside {{#concentrated_in_one_dim}}; {{anomaly_period:period}} inside {{#anomaly_confirmed}}, {{#anomaly_up}} or {{#multiple_anomalies}} (each of them implies a confirmed anomaly). A sentence inside another section names the period through that function's own slot, for example {{change_to_period:period}} inside {{#concentrated_in_one_dim}}. A ratio slot is used only inside {{#anomaly_confirmed}} or {{#change_large}}: {{anomaly_ratio_median:ratio}} with the anomaly, {{change_ratio:ratio}} with change_large; never {{anomaly_ratio_prev}}, which is unset when the anomaly is the first period. A change that is a collapse (a drop to near zero) is described through {{change_pct:pct}}, never through a ratio. Direction words ("increase", "rise", "jump", "times more") appear only inside {{#anomaly_up}} or {{#change_up}}; every other sentence about an anomaly or a change is worded neutrally ("stands out", "changed by {{change_pct:pct}}") and never sits in a {{^anomaly_up}} or {{^change_up}} section, because such a section also shows when there is no anomaly or no change at all. prefix= renames VALUES keys only and is one to ten lowercase letters or digits, starting with a letter, then one underscore (prefix="ovl_", never prefix="loan_amount_"): a slot then carries the prefix ({{ovl_overlap_equal_share:pct}}), a section flag never does, because FINDINGS keys and codes are fixed ({{#concentrated_in_one_dim}}, never {{#change_concentrated_in_one_dim}}); pass prefix= only when two calls in one reply would set the same VALUES key.

PITFALLS (every data answer)
- A stock (active, outstanding, portfolio) summed over months double counts — use the latest period.
- A 'granted' column equal to an 'active' column in most rows is a stock, not new grants — check_metric_overlap.
- Amounts across different currency or unit labels are never added without conversion — mixed_units.
- A thin period (few rows) is a coverage problem, not a business drop — period_status partial_period.
- 'This month' is the calendar month; the data's latest period may be older — latest_is_past_period.
- A dimension dominated by an unknown/placeholder member cannot explain anything — unknown_dominated.

PERIOD SCOPE AND STOCK METRICS (every data answer)
- Period scope: when a number in the narrative is an aggregate over a period column (a sum, count or mean across DATE / month / period rows), the first sentence says which periods it covers, through slots the code sets itself: for a span, {{period_from:period}} to {{period_to:period}} with {{n_periods:count}}; for a single period, {{period:period}} (or {{latest_period:period}}). The code assigns VALUES["period_from"], VALUES["period_to"] and VALUES["n_periods"] after any VALUES.update(...) from pdc_diag, so a pdc_diag n_periods for the whole data never replaces the scope of the answer. Never a bare total without its period scope.
- Stock-like metrics: when the dataset profile's warnings say a metric equals another metric in most rows, or name it as stock-like (active, outstanding, portfolio), a "how many" or "what amount" question on that metric is answered for the latest period only (the latest complete period when coverage_check reports partial_period): the first sentence says so, and the code sets VALUES for that period (including {{period:period}}). Produce a sum across periods only when the user explicitly asks for a total over time, and then the narrative says that the sum counts the same items once per period.

CONVERSATION HISTORY
- Earlier answers appear as templates: {{name}} slots stand for values the user saw and you never see, and a first line "[Findings: key=value; …]" lists that answer's checks. Never guess, restate or compute from a slot's value; refer to it in words ("the total shown above").
````

#### B10.4 `prompts.json["planner_narrative_suffix"]` and `["retry_narrative_system"]` (exact text)

`planner_narrative_suffix`:

````text
NARRATIVE OUTPUT FORMAT (extends the format above): a ```python``` or ```plot_code``` reply — single or ###NEXT_PLOT### multi-block — ends with exactly one ```narrative block AFTER its last code block, optionally followed by ```caption:<n> blocks; the code sets VALUES for every slot the narrative uses. ANSWER, CLARIFICATION, MISSING_DATA and NO_CODE replies carry no narrative block.
````

`retry_narrative_system`:

````text
NARRATIVE: the failed code may set VALUES and FINDINGS for an answer template the client already holds. Keep every VALUES key and every FINDINGS key the failed code sets, with the same meaning and the same kind of value. If your corrected code changes what those values mean, end the reply with a new ```narrative block after the last code block (same template language as the plan: slots {{name:kind}}, sections {{#flag}}…{{/flag}}), optionally followed by ```caption:<n> blocks numbered within THIS reply. Never write a number, name or category value yourself in a narrative or caption.

PDC_DIAG ON RETRY: pdc_diag is pre-imported in every block. Do not import it (a plain "import pdc_diag" only binds the same module again; any other import that binds the name pdc_diag is refused, see below), and never redefine, stub, mock, wrap or replace it; write no fallback branch for it (no try/except around a pdc_diag call, no "if pdc_diag is None", no check of globals(), no replica of its logic). A failed pdc_diag call is fixed by correcting only the argument the error message names (it names the parameter and the column); every other argument stays as written, including to_period="primary_anomaly", a literal string the function resolves itself. For example "dims: column 'X' not found in the frame" ⇒ pass an existing column name under dims; "got an unexpected keyword argument 'dimension'" ⇒ use the parameter name from the signatures below. Signatures (parameters after * are keyword-only): coverage_check(df, period_col, dims, metrics, *, unit_cols=None, order=None, prefix=""); detect_anomalies(df, period_col, metric, agg="sum", *, order=None, prefix=""); decompose_change(df, period_col, metric, from_period=None, to_period=None, dims=(), *, agg="sum", order=None, prefix=""); attribute_level(df, target, dims, numeric_cols, *, prefix=""); check_metric_overlap(df, metric_a, metric_b, *, prefix=""); plot_highlighted_series(table, period_col="period", value_col="value", *, title=None, y_title=None); plot_waterfall(table, *, dim=None, title=None, y_title=None); plot_importance(table, *, title=None, x_title="Share of variation explained"). The client sandbox refuses a block before it runs, with the error "pdc_diag cannot be redefined", when it binds the name pdc_diag in any way other than a plain "import pdc_diag" (import … as pdc_diag, from … import pdc_diag, an assignment, del, a for / with / except target, an attribute write such as pdc_diag.fmt = …), or defines a function or class named pdc_diag or a function named after a pdc_diag function (fmt, coverage_check, detect_anomalies, decompose_change, attribute_level, check_metric_overlap, plot_highlighted_series, plot_waterfall, plot_importance); a stub, mock or wrapper under another name (for example with a suffix such as _mock) and a fallback branch are not refused, but they replace the real findings and are just as invalid. A KeyError from a pdc_diag result lists the available keys; use exactly one of them, do not guess another name.
````

#### B10.5 `skills/core/diagnostics.yaml` (outline; content keys rendered verbatim)

```yaml
id: diagnostics
type: core
token_budget: 900
purpose: >
  Answer why / what-drives / what-problems / what-should-we-do questions with a fixed, deterministic
  playbook run through pdc_diag, and one narrative template whose conclusions are conditionals over FINDINGS.
routing:
  - "Diagnostic and advisory plans ALWAYS use the ###NEXT_PLOT### multi-block form, at most 4 blocks in a pass."
  - "Every block produces a figure (fig) or a RESULT table, and sets VALUES/FINDINGS with pdc_diag results."
  - "Each ###NEXT_PLOT### block runs with its own empty VALUES and FINDINGS and cannot read another block's variables or results, so its code never reads a VALUES or FINDINGS key that another block sets (never branch in code on one). The narrative template may use every block's VALUES and FINDINGS."
  - 'A block that needs the anomaly period obtains it itself: pdc_diag.decompose_change(..., to_period="primary_anomaly") — leave from_period unset and use the same agg as the anomaly check (or run detect_anomalies again inside the block).'
playbook:
  step_1_coverage: >
    First block: pdc_diag.coverage_check(df, <period col>, dims=<categorical dims>, metrics=<metrics>,
    unit_cols=<currency/unit columns>). Its findings decide whether later numbers are comparable.
    RESULT = r["table"]. The narrative opens with the coverage caveat when {{#coverage_change}},
    {{#mixed_units}}, {{#partial_period}} or {{#unknown_dominated}} holds.
  step_2_confirm_anomaly: >
    pdc_diag.detect_anomalies on the metric the question is about; fig = pdc_diag.plot_highlighted_series(r["table"]).
    If {{#no_anomaly}}, the conclusion says plainly that the data shows no unusual period.
  step_3_decompose_or_attribute: >
    A change over time -> pdc_diag.decompose_change (from the period before the anomaly to the anomaly period),
    fig = pdc_diag.plot_waterfall. A level (what drives a rate) -> pdc_diag.attribute_level,
    fig = pdc_diag.plot_importance.
  step_4_conclusion: >
    Write the conclusion as template conditionals over FINDINGS codes: {{#spread_evenly}} (a broad-based change
    across every dimension usually means a change of scope, definition or reporting, not a business event),
    {{#concentrated_in_one_dim}} (name the dimension with the {{change_dim_label:text}} slot),
    {{#dominant_feature}} / {{#weak_signal}}. First sentence = the direct answer with its slot;
    last sentence = how to read the visual.
  keys:
    coverage_check: >
      Call coverage_check(df, period_col, dims=[...], metrics=[...], unit_cols=[...]): dims= and metrics=
      are required, pass both on every call. VALUES n_periods, first_period, latest_period, period_from,
      period_to (the same values as first_period and latest_period), latest_rows, median_rows,
      thin_period_label, coverage_jaccard, unknown_share_max, as_of_period, coverage_dim_label,
      unknown_dim_label, disappeared_label, disappeared_after_period (unknown_dim_label and
      unknown_share_max name the same dim as the unknown_dim finding);
      FINDINGS coverage, coverage_dim, thin_period, period_status, unknown_dominated, unknown_dim, units,
      metric_gap, period_order_unknown, latest_is_current_period, latest_is_past_period,
      label_disappeared (a unit_cols label whose rows end before the last period: a 0 for
      disappeared_label in a later period means no rows, so never print that 0 outside
      {{#label_disappeared}})
    detect_anomalies: >
      Optional rows_col= (the row-count column of pre-aggregated input) and dims= (coverage dims).
      VALUES anomaly_period, anomaly_value, baseline_value, anomaly_ratio_median, anomaly_prev_period,
      anomaly_prev_value, anomaly_ratio_prev, rows_ratio (the anomaly period's rows over the previous
      period's rows), n_periods, period_from, period_to (the first and the last period), n_anomalies;
      FINDINGS anomaly (anomaly_confirmed, no_anomaly, or scope_change: the anomaly period has more
      than 3x or less than 1/3 of the previous period's rows, or a dims= coverage changed, so its
      total reflects a change of reporting scope, not a business change; then anomaly_up and
      anomaly_is_latest are not set: write {{#scope_change}} and compare a rate with
      rate_by_period instead of calling the total a problem), anomaly_up, anomaly_is_latest,
      multiple_anomalies, too_few_periods,
      period_status (data_gap); TABLE period, value, robust_z, ratio_prev, ratio_median, is_anomaly,
      is_primary, rows, is_data_gap. A data-gap period is never flagged or primary, so anomaly_period and
      n_anomalies never count it; a ratio slot shows <0.1x below 0.05 and >1000x above 1000. Chart:
      fig = pdc_diag.plot_highlighted_series(r["table"], y_title="…") — y_title=, never yaxis_title=
    decompose_change: >
      VALUES change_from_period, change_to_period, change_from_value, change_to_value, change_abs,
      change_ratio, change_pct, change_top_member, change_top_member_share, change_top_member_delta,
      change_dim_label (change_ratio and change_pct are None when the from value is 0, so a change_pct slot
      outside {{#change_large}} can be n/a when the change starts from 0; change_dim_label is set exactly
      when change_top_member is); FINDINGS change_shape (concentrated_in_one_dim, spread_evenly, or
      scope_change: the to period is a change of scope, no member is attributed and the
      change_top_member* / change_dim_label values stay None), change_dim, change_up, change_large
      (only when the from value is above 0), unknown_dominated, unknown_dim (the first dim dominated by
      unknown members, left out of the decomposition; it sets no unknown_dim_label, that slot comes from
      coverage_check), too_few_periods, anomaly (no_anomaly when
      to_period="primary_anomaly" finds none, scope_change when it lands on one); optional
      rows_col= as in detect_anomalies
    attribute_level: >
      VALUES top_feature_label, second_feature_label, top_feature_share, second_feature_share, n_rows_used,
      top_member, top_member_target, overall_target; FINDINGS attribution, top_feature, second_feature,
      method_fallback
    check_metric_overlap: >
      VALUES overlap_equal_share, overlap_sum_ratio, overlap_rows, overlap_nonnull_rows; FINDINGS metric_relation, metrics_identical
    rate_by_period: >
      Call rate_by_period(df, period_col, numerator, denominator) for a ratio of two summed metrics
      per period (for example a share of a portfolio); use it when the totals move with the row
      count ({{#scope_change}}). VALUES rate_first, rate_last (fractions: the :pct kind),
      rate_change_pp (percentage points: the :float kind), rate_first_period, rate_last_period,
      period_from, period_to, n_periods; FINDINGS rate_up, rate_down (a move of more than 0.5
      percentage points), too_few_periods; TABLE period, numerator, denominator, rate
    slot_rule: >
      A narrative slot must be one of these VALUES keys (with the prefix= you passed) or a key the code sets
      itself with VALUES["..."] = …; never an invented name.
single_pass_rule: >
  Do all four steps in THIS pass (at most 4 blocks): coverage, anomaly, then decompose_change with
  to_period="primary_anomaly" (or attribute_level), then the conditional conclusion. Never set FINDINGS["needs_phase2"].
phase2_rule: >
  If step 3 is needed and was NOT done in this pass (for example the anomaly is not yet confirmed when you
  write the code), set FINDINGS["needs_phase2"] = bool(<anomaly confirmed>) and do steps 1-2 only; the client
  then asks you once more with the FINDINGS. If you can do step 3 in this pass, do it and never set needs_phase2.
# The brain renders exactly ONE of the two keys above: `phase2_rule` when the request carries two_phase_v1,
# otherwise `single_pass_rule` (the other key is deleted from the dict before rendering). Test:
# test_diagnostics_yaml_single_pass_without_two_phase.
advisory: >
  Same steps; the conclusion is framed as recommendation conditionals ("{{#concentrated_in_one_dim}}Focus on
  … {{/concentrated_in_one_dim}}"). Never invent targets, thresholds, benchmarks or amounts; every number is a slot.
template_patterns:
  - "{{#coverage_change}}Note: the set of {{coverage_dim_label:text}} values changes between periods, so totals are not comparable.{{/coverage_change}}"
  - "{{#mixed_units}}Note: the amounts carry more than one currency or unit label, so they are reported per label and never added together.{{/mixed_units}}"
  - "{{#metric_definition_overlap}}Note: in {{overlap_equal_share:pct}} of rows the two metrics are equal, so the one asked about counts a stock (items still active), not new items, and adding periods together counts the same items more than once.{{/metric_definition_overlap}}"
  - "{{#anomaly_confirmed}}{{anomaly_period:period}} stands out at {{anomaly_ratio_median:ratio}} the typical level.{{/anomaly_confirmed}}"
pitfalls:
  - "A stock (active, outstanding, portfolio) summed over months double counts — use the latest period."
  - "A 'granted' column equal to an 'active' column in most rows is a stock, not new grants — check_metric_overlap."
  - "Amounts across different currency or unit labels are never added without conversion — mixed_units."
  - "A thin period (few rows) is a coverage problem, not a business drop — period_status partial_period."
  - "'This month' is the calendar month; the data's latest period may be older — latest_is_past_period."
  - "A dimension dominated by an unknown/placeholder member cannot explain anything — unknown_dominated."
data_gaps:
  - "A period pdc_diag reports as thin (FINDINGS thin_period), as a partial latest period (period_status = partial_period, from coverage_check) or as a data gap (period_status = data_gap, from detect_anomalies: a period with too few rows, or a value collapsed to 0 between non-zero neighbours) is a data gap — fewer rows were loaded for it, or its value collapsed — and is described as such: inside {{#thin_period}} with {{thin_period_label:period}}, inside {{#partial_period}} with {{latest_period:period}}, inside {{#data_gap}} as a period with too few rows or a collapsed value, with no period slot (detect_anomalies sets none); never as an anomaly, a drop, a spike or a business change. period_status holds one code: in the merged FINDINGS a later block's data_gap replaces an earlier block's partial_period, so the thin period is also told inside {{#thin_period}}, which coverage_check keeps."
  - "A dimension pdc_diag reports as unknown_dominated is never used to explain a change: the block that calls decompose_change or attribute_level runs pdc_diag.coverage_check itself, without merging that call into VALUES or FINDINGS, and removes r[\"findings\"].get(\"unknown_dim\") from its decompose_change dims and attribute_level dims; the template never names it as a driver, and its only sentence is inside {{#unknown_dominated}} with {{unknown_dim_label:text}}."
  - "When detect_anomalies reports multiple_anomalies, the template says that several periods stand out and lists none by name unless a slot exists for each one; inside {{#multiple_anomalies}} only {{anomaly_period:period}} is named, as the primary one, and the sentence says so."
forbidden:
  - "No numbers, names or category values in the template; no FINDINGS key built from data; no third pass."
```

Pinned sentinels in `tests/test_skills_yaml.py`: `"###NEXT_PLOT###"`, `"coverage_check"`, `"needs_phase2"`, `"Never invent targets"`; `data_gaps` (three bullets, `"period_status = data_gap"`, `"{{#data_gap}}"`) and the per-function `playbook.keys` sentinels have their own tests there.

#### B10.6 `/v1/summarize` reading guide (D11)

In `summarize_answer` (`brain_agent.py:2162-2232`), when `result_json == "NONE"`, insert the following right after the `Question:` part (`:2204`):

```text
READING GUIDE (no result value):
No result value is available to you. Do not state, estimate or guess any number, total, percentage, name or category. In 1-2 sentences in the answer language, say what the result shows (which metric, which grouping/period, from the question and schema) and how to read it. The value itself is displayed to the user separately. This overrides any example with numbers in your instructions.

```

- Log `SUMMARIZE_READING_GUIDE`.
- A non-null preview ⇒ byte-identical prompt.
- Behaviour change: old clients' null previews also get the guide (documented in PROTOCOL).

#### B10.7 `/v1/describe` findings block

```python
def _findings_block(data_caveat) -> str:   # "" when absent/malformed/empty
```

Appended after `{caveat_block}` (`brain_agent.py:2050`):

```text


DIAGNOSTIC FINDINGS (names and codes only — no values):
- <key>: <true|false|value>
These codes come from deterministic checks the client ran on the executed result. You may say in one short clause what a code means for reading this chart (for example that the set of currencies changes between periods). Never state, estimate or guess a number, a name or a category value; only the column names listed here may be named.
```

- The keys are sorted, and the block is not subject to `[[DATA_NOTE]]`.
- `routes/llm.py` describe (`:230-232`) accepts `data_caveat` when it is a dict.
- `_data_caveat_block` still requires `kind`. `_findings_block` reads `data_caveat.get("findings")` through `_clean_findings`.
- Log `DESCRIBE_FINDINGS_APPLIED n=`.

#### B10.8 `_findings_prompt_block(findings)` (exact text; non-empty cleaned findings only)

```text
PHASE 2 OF 2 — the client already ran your phase-1 code (shown as the last history turn) for this same question. Its deterministic checks returned these FINDINGS (codes and column names only; you never see values):
- <key>: <true|false|value>
Write ONLY the follow-up analysis — step 3 of the diagnostic playbook: pdc_diag.decompose_change for a change over time (pass to_period="primary_anomaly" unless anomaly_is_latest is true), pdc_diag.attribute_level for a level — as ###NEXT_PLOT### blocks; a block that calls pdc_diag.plot_* is a ```plot_code block. Do not repeat the coverage or anomaly checks. End with your own ```narrative block (optionally ```caption:<n> blocks numbered within THIS reply); its first sentence answers what drives the result. Never set FINDINGS["needs_phase2"]: there is no third pass.

```

Log `PHASE2_PLAN findings_n=`.

---

## C. Worked example and task list

### C11. Worked example — BRB Q3–Q6

**The file.** One uploaded monthly loan-facts file with the columns `DATE`, `GENDER`, `TYPE`, `SEKTOR`, `SEGMENT`, `REGION`, `DISTRICT`, `CURRENCY`, `PERC_RATE`, `LOAN_COUNT`, `LOAN_AMOUNT`, `NEW_LOAN_COUNT`, `NEW_LOAN_AMOUNT`, `GRANTED_LOAN_COUNT`, `GRANTED_LOAN_AMOUNT`, `NPL_COUNT`, `NPL_AMOUNT`.

**Known pathologies** (no figures here):
- `DATE` labels are "year + month name";
- the currency label of every row switches to "other currencies" from the third month;
- `SEKTOR` becomes the placeholder "unknown" in the same months;
- one month is thin;
- the last month shows a large broad-based jump;
- `GRANTED_LOAN_COUNT` equals `LOAN_COUNT` (an active stock) in most rows, while `NEW_LOAN_COUNT` is small.

All templates below are English for readability. In production they are written in Uzbek, the answer language.

#### Q3 "nechta kredit ajratildi?" (how many loans were allocated)

- **Classification:** `aggregation` (not diagnostic) ⇒ multi flow (chart + table), no phase 2.
- **Block 1 (`python`, table):**
  - `cov = pdc_diag.coverage_check(df, "DATE", dims=["CURRENCY","SEKTOR","REGION"], metrics=["GRANTED_LOAN_COUNT"], unit_cols=["CURRENCY"])`;
  - `ov = pdc_diag.check_metric_overlap(df, "GRANTED_LOAN_COUNT", "LOAN_COUNT")`;
  - monthly totals in chronological order;
  - `VALUES`: `total_granted` (sum), `latest_granted` (latest month), `total_new` (`NEW_LOAN_COUNT` sum), plus the `cov`/`ov` values;
  - `FINDINGS.update(cov["findings"]); FINDINGS.update(ov["findings"])`;
  - `RESULT` = the monthly table.
- **Block 2 (`plot_code`):** `r = pdc_diag.detect_anomalies(df, "DATE", "GRANTED_LOAN_COUNT")`; `fig = pdc_diag.plot_highlighted_series(r["table"])`; `VALUES.update(r["values"])`.
- **FINDINGS (validated):** `coverage=coverage_change; coverage_dim=CURRENCY; units=mixed_units; unknown_dominated=true; unknown_dim=SEKTOR; thin_period=true; latest_is_current_period=false; latest_is_past_period=true; metric_relation=metric_definition_overlap`, plus the detect_anomalies keys from block 2. This stays under the 24-key cap after the merge.
- **Narrative:**

  ```
  {{#metric_definition_overlap}}In most rows GRANTED_LOAN_COUNT equals LOAN_COUNT (active loans), so it counts the active portfolio, not new grants: in {{latest_period:period}} there were {{latest_granted:count}} such loans, and {{total_new:count}} new loans were issued over {{n_periods:count}} months. Adding the months together would count the same loans several times.{{/metric_definition_overlap}}{{^metric_definition_overlap}}{{total_granted:count}} loans were granted over {{n_periods:count}} months ({{first_period:period}} – {{latest_period:period}}).{{/metric_definition_overlap}}{{#thin_period}} One month has far fewer rows than the others ({{thin_period_label:period}}), so it may be incomplete.{{/thin_period}} The bars show the count per month; the highlighted bar is the month that stands out.
  ```

- **`caption:2`:** "Count per month; {{anomaly_period:period}} is about {{anomaly_ratio_median:ratio}} the typical month." Block 1 has no caption, so its table gets today's per-chart describe (A6).

#### Q4 "qancha miqdorda kredit ajratildi" (what amount was allocated)

- **Classification:** `aggregation`; multi flow; no phase 2.
- **Block 1 (`python`):**
  - `coverage_check(..., metrics=["GRANTED_LOAN_AMOUNT"], unit_cols=["CURRENCY"])`;
  - amounts per month × currency label;
  - `VALUES`: `amount_latest` (the latest month, under its own label) and `amount_first_label_total` (the sum over the months that carry the national-currency label);
  - `RESULT` = the month × currency table.
- **Block 2 (`plot_code`):** grouped bars per month, coloured by currency label, with the y-axis titled "Amount (file units, not converted)".
- **FINDINGS:** `units=mixed_units; coverage=coverage_change; coverage_dim=CURRENCY; metric_relation=metric_definition_overlap` (the overlap check runs on `GRANTED_LOAN_AMOUNT` vs `LOAN_AMOUNT`).
- **Narrative:**

  ```
  {{#mixed_units}}The months carry different currency labels and the file does not convert them, so one total would add different units. {{/mixed_units}}In {{latest_period:period}} the granted amount was {{amount_latest:amount}}{{#coverage_change}} (all of it under a different currency label than the first months){{/coverage_change}}.{{#metric_definition_overlap}} As with the counts, the "granted" amount behaves like the active portfolio, so months must not be added.{{/metric_definition_overlap}} Each bar is one month and currency label.
  ```

- **Captions:** `caption:1` "Amount per month and currency label." and `caption:2` "Bars are not converted between currencies."

#### Q5 "bu malumotlardan kelib chiqib qanday muamolar mavjud ekan" (what problems are there)

- **Classification:** `diagnostic` ⇒ `diagnostics.yaml` is loaded, and the plan returns `diagnostic: true`.
- **Phase 1** (three blocks):
  1. `coverage_check(df, "DATE", dims=["CURRENCY","SEKTOR","SEGMENT","REGION","DISTRICT"], metrics=["GRANTED_LOAN_COUNT","NPL_COUNT","LOAN_AMOUNT"], unit_cols=["CURRENCY"])` → table;
  2. `detect_anomalies(df, "DATE", "GRANTED_LOAN_COUNT")` → `plot_highlighted_series`;
  3. `check_metric_overlap(df, "GRANTED_LOAN_COUNT", "LOAN_COUNT")` + a bar chart of the NPL ratio per month (VALUES `npl_ratio_latest` as a fraction, prefix `npl_`).

  The code sets `FINDINGS["needs_phase2"] = bool(r["findings"].get("anomaly") == "anomaly_confirmed")` with a literal key.
- **Phase-1 FINDINGS:** `coverage=coverage_change; coverage_dim=CURRENCY; units=mixed_units; unknown_dominated=true; unknown_dim=SEKTOR; thin_period=true; latest_is_current_period=false; latest_is_past_period=true; anomaly=anomaly_confirmed; anomaly_up=true; anomaly_is_latest=true; metric_relation=metric_definition_overlap; needs_phase2=true`.
- **Phase-1 narrative** (each bullet sits inside its section):

  ```
  The data shows {{#anomaly_confirmed}}one month that stands out ({{anomaly_period:period}}, about {{anomaly_ratio_median:ratio}} the typical level){{/anomaly_confirmed}}{{^anomaly_confirmed}}no unusual month{{/anomaly_confirmed}} and these data-quality problems:
  {{#coverage_change}}- the currency label changes between months, so amounts are not comparable
  {{/coverage_change}}{{#unknown_dominated}}- {{unknown_dim_label:text}} is mostly "unknown" in later months, so it cannot explain anything there
  {{/unknown_dominated}}{{#thin_period}}- {{thin_period_label:period}} has far fewer rows than the other months
  {{/thin_period}}{{#metric_definition_overlap}}- "granted" counts equal the active portfolio in most rows
  {{/metric_definition_overlap}}The highlighted bar marks the unusual month; the next step checks what drives it.
  ```

- **Phase 2:**
  - The request carries the FINDINGS above.
  - The reply is one block: `decompose_change(df, "DATE", "GRANTED_LOAN_COUNT", dims=["REGION","TYPE","GENDER"])` → `plot_waterfall` (`from_period`/`to_period` default to the last two months).
  - **FINDINGS:** `change_shape=spread_evenly; change_up=true; change_large=true`.
  - **Phase-2 narrative:**

    ```
    {{#spread_evenly}}The jump in {{change_to_period:period}} is spread evenly across every region, borrower type and gender ({{change_ratio:ratio}} the previous month). A rise that uniform usually means the month was reported with a different scope or definition, not that lending changed — confirm with the data owner before using it.{{/spread_evenly}}{{#concentrated_in_one_dim}}Most of the change comes from one {{change_dim_label:text}}: {{change_top_member:text}} ({{change_top_member_share:pct}} of the change).{{/concentrated_in_one_dim}} The waterfall shows each group's contribution from the previous month to this one.
    ```

  - Any `needs_phase2` set in phase 2 is ignored.

#### Q6 "bu oyda nechta yangi kreditlar ajratilgan" (how many new loans this month)

- **Classification:** `aggregation`; the single flow is acceptable (one `python` block plus a table).
- **Code:**
  - `cov = pdc_diag.coverage_check(df, "DATE", dims=["CURRENCY"], metrics=["NEW_LOAN_COUNT"])`;
  - `latest = cov["values"]["latest_period"]`;
  - `VALUES["new_latest"] = int(df[df["DATE"] == latest]["NEW_LOAN_COUNT"].sum())`;
  - `RESULT` = the per-segment table for that month;
  - `FINDINGS.update(cov["findings"])`.
- **FINDINGS:** `latest_is_current_period=false; latest_is_past_period=true`, plus the coverage keys.
- **Narrative:**

  ```
  {{#latest_is_past_period}}The file has no data for the current month; its latest month is {{latest_period:period}}. {{/latest_is_past_period}}In {{latest_period:period}}, {{new_latest:count}} new loans were issued (NEW_LOAN_COUNT — new loans, unlike the granted/active counts above). The table splits them by customer segment.
  ```

- **`caption:1`:** "New loans by segment in {{latest_period:period}}."

### C12. Task list — three sessions

#### Session 1 — Client (narrative, VALUES/FINDINGS, pdc_diag, storage, display; no two-phase)

**Files:**
- new `pdc_diag.py` and `narrative.py`;
- `code_exec.py` (`:56-57`, `:231-269`, `:314-327`);
- `plot_utils.py` (`:21`, `:360-404`, `:1916`, the four success returns);
- `exec_transport.py` (new functions, `:916-918`, `:993-1017`);
- `run_chat_local.py`: `_NarrState`, single and multi flows, the `_describe_with_backstop` findings kwarg, the per-chart describe for missing captions, summarize `preview=None` at `:981` / `:2187`, `value_display`, `block_index` in the worklist;
- `brain_client.py`: `plan`/`retry` get `capabilities=None` and `findings=None`, with payload keys only when truthy; `_compact_findings_for_transport`; `_compact_caveat_for_transport` handles findings;
- `settings.py`: `BRAIN_CAPABILITIES`, default `"narrative_v1,diagnostics_v1"`, parsed per call; empty ⇒ nothing sent. Ownership:
  - only `run_chat_multi_plot` reads the setting (covering its single path, phase 2 and every retry from it) and passes `capabilities=` explicitly;
  - `brain_client` never reads it;
  - `_run_chat_body` (Auto Analytics, `run_chat_local.py:805`) therefore sends none and appends no `value_display`, because its `text` feeds `/v1/report` through `auto_analytics.py:207`;
- `routes/chat.py`: `_apply_narrative_fields`, `_build_chart_entry`, both builders, worker accumulation, the consumer forwarding `phase` / narrative / tables partials, edit-regenerate `record_view`, the title via `brain_view`;
- `routes/report.py`: `_build_qa_pairs` and the four local fallbacks;
- `static/dashboard.js`: `renderAiRecord`, `appendNarrativeMessage`, the narrative slots, single-done narrative/caption, table captions, and edit-regenerate and recover through `renderAiRecord`;
- `static/dashboard.css`;
- `executor/Dockerfile:67` and `Dockerfile:72-80`.

**Tests:**
- `tests/test_pdc_diag_fmt.py`: `test_fmt_examples_table`, `test_fmt_never_scientific`, `test_fmt_bool_before_int`, `test_fmt_na_inputs`, `test_fmt_long_number_na`, `test_fmt_negative_zero`, `test_fmt_unknown_locale_is_default`.
- `tests/test_pdc_diag.py`: `test_order_periods_datetime`, `test_order_periods_month_names_en_ru_uz_ka`, `test_order_periods_explicit_order`, `test_order_periods_unknown_sets_flag`, `test_coverage_check_member_set_change`, `test_coverage_check_partial_latest_period`, `test_coverage_check_unknown_dominated`, `test_coverage_check_mixed_units`, `test_coverage_check_latest_is_current_period_as_of`, `test_detect_anomalies_relative_spike`, `test_detect_anomalies_flat_no_anomaly`, `test_detect_anomalies_too_few_periods`, `test_decompose_change_concentrated`, `test_decompose_change_spread_evenly`, `test_decompose_change_default_last_two`, `test_attribute_level_dominant_feature`, `test_attribute_level_weak_signal`, `test_attribute_level_shap_fallback_flag`, `test_check_metric_overlap_identical`, `test_prefix_applies_to_values_only`, `test_findings_keys_in_registry`, `test_findings_values_bool_enum_or_column_only`, `test_missing_column_raises_value_error`, `test_plot_helpers_return_one_plotly_figure`, `test_pdc_diag_top_level_imports_are_leaf` (AST).
- `tests/test_narrative_render.py`: `test_slot_basic`, `test_slot_kinds`, `test_unknown_kind_uses_default`, `test_missing_slot_na_logs_name_only`, `test_section_positive_bool`, `test_section_positive_non_bool`, `test_section_enum_code_matches_value`, `test_section_negative`, `test_nested_section_fallback`, `test_unclosed_section_fallback`, `test_mismatched_close_fallback`, `test_non_grammar_braces_literal`, `test_control_chars_stripped`, `test_render_caps_cut_at_whitespace`, `test_template_over_cap_rejected`, `test_render_never_raises_fuzz`, `test_brain_view_brackets_slots_resolves_sections`.
- `tests/test_findings_validator.py`: `test_key_must_be_quoted_literal`, `test_pdc_diag_keys_allowed_without_literal`, `test_value_bool_column_enum_only`, `test_data_built_key_dropped`, `test_cap_24_priority_needs_phase2_then_registry`, `test_merge_in_block_index_order_not_execution_order`, `test_column_value_with_marker_chars_dropped`, `test_marker_sorted_bool_text_and_400_cap`, `test_merge_later_wins_logs_collisions`.
- `tests/test_values_capture.py`: `test_encode_values_types_keys`, `test_encode_values_numpy_item`, `test_encode_values_cap_40`, `test_decode_values_hostile`, `test_python_site_captures_values_findings`, `test_plot_site_captures_on_all_success_returns`, `test_global_plot_scope_not_mutated`, `test_reconstruct_keeps_new_keys`.
- `tests/test_narrative_boundary.py`: `test_values_never_in_plan_payload`, `test_values_never_in_retry_payload`, `test_values_never_in_describe_payload`, `test_values_never_in_report_payload`, `test_values_never_in_title_payload`, `test_summarize_preview_always_null`, `test_history_rows_strip_new_fields`, `test_capabilities_only_when_configured`. Each asserts that the sentinel VALUE 987654321 and the text "SENTINEL_CATEGORY" are absent from every `json.dumps(payload)`.
- `tests/test_narrative_flow.py`: `test_single_narrative_skips_describe_and_summarize`, `test_single_retry_narrative_replaces_plan`, `test_single_retry_absent_narrative_keeps_plan`, `test_single_caption_only_text_is_caption_template`, `test_caption_uses_own_block_values_only`, `test_multi_caption_index_counts_table_blocks`, `test_multi_missing_caption_describes_that_chart_only`, `test_multi_no_narrative_captionless_chart_describe_with_findings`, `test_backstop_sentence_prepended_locally`, `test_values_merge_order_collision_log`, `test_failed_block_slot_renders_na`, `test_no_successful_block_drops_narrative`, `test_fallback_value_display_scalar_and_dict`, `test_old_brain_response_unchanged_flow`.
- `tests/test_narrative_records.py`: `test_single_record_fields`, `test_multi_images_caption_template_and_phase`, `test_content_is_marker_plus_template`, `test_stopped_record_uses_templates`, `test_edit_regenerate_record_view_fields`, `test_old_shape_record_fixture_loads` (`tests/fixtures/history_pre_narrative.jsonl`), `test_title_gets_brain_view`, `test_report_brain_text_vs_local_display`.
- `tests/test_narrative_sse.py`: `test_narrative_event_does_not_end_stream`, `test_unknown_event_dropped_and_logged`, `test_edit_regenerate_answer_is_display_text`, `test_cancel_done_uses_display_answer`, `test_report_paragraph_text_escaped`, `test_report_table_columns_only_input_names`, `test_compact_caveat_findings_without_kind_kept`, `test_describe_facts_hint_stripped_under_narrative`.
- `tests/test_narrative_ui_structure.py`: `test_render_ai_record_shared_by_reload_recover_edit`, `test_narrative_rendered_before_visuals`, `test_narrative_and_caption_through_render_markdown`, `test_old_record_path_unchanged`.
- Updated: `tests/test_executor_structure.py` (`RUNTIME_COPY_SOURCES` + `pdc_diag.py`) and `tests/test_executor_dispatch.py:80`. New: `test_pdc_diag_registered_at_both_exec_sites` and `test_pdc_diag_not_in_sandbox_denylist`.

**Docs:**
- `docs/CLIENT_ENDPOINTS.md`: SSE events, the namespace (`pdc_diag` / VALUES / FINDINGS), record fields;
- `docs/EXECUTOR_PROTOCOL.md` §5/§6: the four keys and their decode rules;
- `docs/AI_CONSTITUTION.md` Art. II: capabilities, findings, null preview, templates in history;
- `docs/ENTERPRISE_ARCHITECTURE.md`: decision entry;
- `CLAUDE.md` file table: `pdc_diag.py`, `narrative.py`.

**Also in session 1 (decisions F1, F2, F19):**
- the value scrubs of B7.6 (`result_backstop.py`, `brain_client._compact_caveat_for_transport`, `brain_client._compact_profiles_for_transport`);
- the error_msg scrub and the fixed `NearZeroChartError` sentence of B7.7 (`brain_client.py`, `plot_utils.py:2040`);
- the markdown escaping of string VALUES (`narrative.py`, `static/dashboard.js renderMarkdown`, `narrative.plain`).

Their tests are named in B7.6, B7.7 and A2.2. Docs: `AI_CONSTITUTION.md` Art. II (both copies) — `dataset_profile` and `data_caveat` no longer carry values, and `error_msg` is scrubbed.

**Definition of done:**
- the full suite is green;
- against an old brain (no `narrative`) the UX is today's, except the null summarize preview + `value_display`;
- `/smoke-test` on :8091 against a new brain;
- a reviewer agent pass on the data boundary.

**Session 1 implementation notes (deliberate deviations; the code is authoritative):**
- **Return shapes at the exec sites.** `code_exec._execute_in_process` writes the four keys EXPLICITLY in its success return literal (`"values": outputs["values"], "values_dropped": …`), not `**outputs`; each success return of `plot_utils._render_in_process` is `out = {...}; return _with_outputs(out, env)`. Both keep the success dicts literals with constant string keys, which the AST pins in `tests/test_executor_dispatch.py` read (`test_the_transport_python_key_list_matches_the_in_process_success_return` compares code_exec's success key set with `exec_transport._PYTHON_KEYS`; `test_the_transport_plot_key_list_covers_every_in_process_return` checks plot_utils' literals against `_PLOT_KEYS`).
- **`.item()` only on numpy scalars.** `exec_transport` converts a value through numpy's own `np.generic.item` only when it is an `np.generic`, and inspects EXACT types (`type(x) is …`, an exact `dict` — or, sandbox side, pdc_diag's own `_ResultDict` — read through `dict.items`). An arbitrary object with an `.item()` method — or a subclass overriding `item()`, `items()` or `__len__` — is dropped without any of its methods running: the capture runs after the timed exec, where code-defined methods could loop forever. (A3.1 said "a non-builtin with a callable `.item()`".)
- **Retry `error_msg` scrub (B7.7).** `_scrub_error_msg` drops a `b` / `u` / `r` / `f` string prefix before matching, so a `b'…'` literal is scrubbed too; replaces a literal whatever its length (no 200-character limit); and KEEPS, besides the input column and df names, any literal the failed code itself contains — as a whole identifier token (dotted names too) or inside one of the code's own string literals — since the brain wrote that text (`NameError: name 'totl'`, the `'North'` of `df.loc['North']`).
- **Stock-vs-flow profile warning (not in this spec).** `dataset_profile` adds the warning `<a> equals <b> in <pct>% of rows: likely a stock, not a flow` for two numeric (non-bool, non-constant) columns equal in more than 90 % of the COMPARED rows — both set and not both zero (`np.isclose`, `rtol=1e-9`); the percentage is over those rows, and a pair with fewer than 10 % of its both-set rows compared is not judged (`EQUAL_PAIR_MIN_COMPARED_SHARE`), so two sparse columns that are zero in most rows never warn; one warning per equality group (k mutually equal columns warn once), two all-unique columns equal in every row (ids, row numbers) are skipped, and the scan is skipped above 30 numeric columns. Names and an integer percentage only. The zero rule raised `dataset_profile.PROFILE_VERSION` to 2: a stored profile of version 1 is read as missing, so uploaded files and snapshot tables recompute theirs at the next question, while a LIVE table's sampled profile is omitted from the plan until the administrator re-profiles the table (Refresh now, or a mode change).
- **`two_phase_v1` filtered out.** `settings.brain_capabilities()` recognises only `narrative_v1` and `diagnostics_v1`; a configured `two_phase_v1` is dropped, and without `narrative_v1` nothing is sent at all (`diagnostics_v1` alone is not announced).
- **Single-chart multi turns.** A `run_chat_multi_plot` turn that produced exactly one chart is stored as a single-visual record (no `images[]`, unless that chart is phase 2), so the chart's caption goes into the record's `caption_template` / `caption_rendered` (an uncaptioned chart of a narrated turn: `caption_described`); a stopped single-chart record appends the stop note to that field.
- **Title.** `_answer_for_title` uses `brain_view` of the narrative template, else (single flow) of the caption template; a multi turn without a narrative passes its brain-safe `content` with any caption template in it bracketed by `brain_view`.
- **Edit-regenerate.** It builds the record with the same `_build_ai_history_record` as the stream and answers `record_view` (the persisted record as the history route serves it) plus `ai_index`; the page renders EVERY chart, the narrative and the table captions from `record_view` through `renderAiRecord`.
- **Smaller points.** Tables partials are accepted by the consumer, the worker and the page but not emitted by the session-1 generator (tables stay on `done`). `/v1/report` `table_columns` keeps nothing when the input column names cannot be read (fail closed). `_NarrState` also carries the bookkeeping the flows need (`sid`, `captions_used`, `described`, `display_answers`, `rendered`).

#### Session 2 — Brain (capabilities, extraction, prompts, classifier, YAML, summarize, describe)

**Files:**
- `brain_agent.py`: `_clean_capabilities`, `_extract_narrative`, `_attach_narrative`, `_clean_findings`, `_findings_prompt_block` (generic listing + phase-2 text), `_findings_block`, classifier gating, `_DATA_TASK_TYPES`, the diagnostics gate, the plan/retry ordering, the summarize reading guide;
- `routes/llm.py`: `capabilities`, `findings`, logs;
- `prompts.json`: three new keys;
- `skills/core/diagnostics.yaml`.

**Tests:**
- `tests/test_narrative_extract.py`: `test_extract_basic`, `test_caption_info_bounds`, `test_first_fence_wins_dup_counted`, `test_template_over_cap_dropped_not_truncated`, `test_caption_over_cap_dropped`, `test_malformed_like_fence_stripped`, `test_piece_join_idiom`, `test_narrative_before_code_not_parsed_as_python`, `test_extract_never_raises`.
- `tests/test_narrative_plan.py`: `test_plan_without_capabilities_is_byte_identical` (absent / None / [] / "x" / ["bogus"]), `test_retry_without_capabilities_is_byte_identical`, `test_narrative_v1_system_and_suffix_order` (golden), `test_response_narrative_and_string_caption_keys`, `test_narrative_dropped_for_text_kinds`, `test_caption_out_of_range_dropped`, `test_fence_without_capability_stripped_no_field`, `test_live_and_narrative_both_stripped`, `test_retry_returns_narrative_captions`, `test_diagnostic_flag_matrix`, `test_diagnostics_v1_needs_narrative_v1`, `test_findings_block_and_no_diagnostic_flag`, `test_findings_cleaning`, `test_response_key_sets` (`PRE_NARRATIVE_KEYS`).
- `tests/test_classifier_diagnostic.py`: `test_classifier_prompt_byte_identical_without_capability`, `test_classifier_section_with_capability`, `test_examples_four_languages`, `test_data_task_types_include_new`, `test_fallback_complexity_unchanged`.
- `tests/test_skills_yaml.py` (additions): `test_diagnostics_yaml_loads_with_sentinels`, `test_diagnostics_loaded_only_when_gate_passes`, `test_diagnostics_skill_name_stripped_without_gate`.
- `tests/test_prompts_json.py` (additions): `test_narrative_keys_present_with_sentinels`, `test_planner_system_bytes_unchanged` (sha256 pinned).
- `tests/test_summarize_reading_guide.py`: `test_null_preview_adds_guide`, `test_non_null_preview_byte_identical`, `test_guide_logged`.
- `tests/test_describe_findings.py`: `test_findings_block_separate_from_data_note`, `test_no_findings_byte_identical`, `test_findings_without_kind_accepted`, `test_malformed_findings_ignored`.
- Golden prompts: `tests/golden/narrative_plan_system.txt`, `narrative_plan_prompt.txt`, `narrative_retry_system.txt`, `classifier_diagnostic.txt`, `summarize_reading_guide.txt`, `findings_block_plan.txt`. Regenerate with `python tests/test_narrative_plan.py --regen`.

**Docs:**
- `docs/PROTOCOL.md`: promote the DRAFT to normative sections in both copies;
- `CLAUDE.md` file table;
- `docs/ENTERPRISE_ARCHITECTURE.md`.

**Also in session 2:**
- the `two_phase_v1` gate (`_clean_capabilities`, the `diagnostic` flag condition, `single_pass_rule` vs `phase2_rule` in diagnostics.yaml);
- `_profile_full_block` without min/max/top_values (B7.6).

**Native-speaker check:** the session's final report lists, verbatim, every Uzbek, Russian and Georgian classifier example of B10.2 (and any other non-English prompt text it added), so a native speaker can check them before deploy.

**Definition of done:**
- the full suite is green and the existing golden files are unchanged;
- deployed first (`/deploy`);
- an old client (no `capabilities`) sees byte-identical prompts and responses;
- a live smoke with `LLM_DEBUG_LOG` briefly ON confirms the fences.

#### Session 3 — Two-phase integration (both repos) + end-to-end

> **Status: decide after testing sessions 1+2 on the BRB questions.** The design (B9, B10.8, the `diagnostic` flag, the phase-2 history row of F4) stays in this spec. Until this session ships, the client never sends `two_phase_v1`, so every diagnostic answer is single-pass (`single_pass_rule`). Shipping this session = implementing B9 and adding `two_phase_v1` to the client default `BRAIN_CAPABILITIES`.

**Files:**
- Client `run_chat_local.py`: `_TurnCtx`, `_run_blocks`, the phase orchestration, the constants `PHASE2_BUDGET_S = 90`, `PHASE2_MAX_RETRIES = 1` and `PHASE2_START_MAX_ELAPSED_S = 120`, the tables partial, the progress event.
- Client `routes/chat.py`: phase fields, `phase2`, `phase2_status`, stopped-record phase handling, consumer forwarding.
- Client `static/dashboard.js`: slot 2, the phase-2 progress, `phase`-aware `renderAiRecord`.
- Client `static/i18n.js`: `lab.phase2_progress` (en/geo/ru).
- Brain: the `diagnostic` flag, the phase-2 text in `_findings_prompt_block`, golden `tests/golden/phase2_plan_prompt.txt`.

**Tests:**
- `tests/test_two_phase.py`: `test_phase2_runs_on_flag_and_needs_phase2`, `test_no_phase2_without_flag`, `test_phase2_skipped_single_flow_logged`, `test_phase2_request_same_history_plus_findings`, `test_phase2_needs_phase2_ignored`, `test_phase2_budget_abandons`, `test_phase2_one_retry_per_block`, `test_phase2_failure_reasons`, `test_phase2_failure_keeps_streamed_visuals_drops_narrative`, `test_phase1_tables_emitted_before_phase2`, `test_event_order`, `test_turn_time_skip`, `test_record_phase2_fields_and_image_phase`, `test_history_content_two_markers`, `test_stop_before_phase2`.
- `tests/test_chat_stream_phases.py`: `test_sse_narrative_event_shape`, `test_sse_phase2_progress_event`, `test_sse_tables_partial_persisted_keys`.
- `tests/test_phase_ui_structure.py`: `test_slots_created_and_filled`, `test_phase2_progress_uses_i18n_key`.
- Brain `tests/test_phase2_prompt.py`: `test_phase2_block_text_golden`, `test_no_diagnostic_on_phase2_request`.

**End-to-end check:**
1. Generate the synthetic fixture `tools/fixtures/loans_synthetic.csv` with `tools/make_loans_fixture.py`. It is seeded, uses invented magnitudes and BRB column names, and has the C11 pathologies.
2. On the local Docker stacks (brain :8090, client :8091), walk Q3–Q6 in Uzbek.
3. Confirm:
   - the narrative appears above the visuals;
   - no sentinel number appears in the brain logs (`LLM_DEBUG_LOG` ON briefly);
   - phase 2 fires only on Q5;
   - reload renders identically;
   - the PDF/PPTX export shows the rendered text.

**Docs:** CLIENT_ENDPOINTS (phase events), PROTOCOL (`diagnostic`, phase-2 `findings`), constitution Art. II bound.

**Definition of done:** both suites green; the e2e checklist recorded in the PR; a ui-tester pass on `/lab`.

---

## D. Article II analysis

| Endpoint | Before | After |
|---|---|---|
| `/v1/plan` | question, schema_text, df_names, history (role/content/code), common_fields, dataset_profile, live_tables | + `capabilities` (constant list); + `findings` (phase 2 only: ≤24 keys, bool / input column name / ENUM); the history `content` of new AI rows is the findings marker + template (the brain's own text with slots) instead of describe/summarize prose |
| `/v1/retry` | as today (failed_code, error_msg, df_columns, …) | + `capabilities`; `failed_code` may contain `VALUES[...] =` / `FINDINGS[...] =` assignments (code, not values) |
| `/v1/describe` | question, code, optional data_caveat | + optional `data_caveat.findings` (validated FINDINGS), only when describe runs (no narrative, or a narrative with a missing caption) |
| `/v1/summarize` | preview = scalar or flat dict (values) | preview always `null` (strictly less) |
| `/v1/title` | first 300 chars of the answer text (could hold a value written by an old summarize) | `brain_view(template)` or value-free content |
| `/v1/report` | answer_text = content / images[i].answer; table_columns = RESULT table labels | `brain_view` of narrative/caption templates, never rendered text; `table_columns` filtered to input column names (pivot/crosstab labels removed) |

**Where VALUES live.** VALUES exist only in:
- the sandbox env;
- `capture_outputs` → the job response;
- `_reconstruct` → `exec_out["values"]`;
- `_NarrState.values`;
- `narrative.render`;
- the record fields `values`, `*_rendered`, `images[i].answer`, `value_display`;
- the browser.

Every brain payload is built from named arguments. History goes through the `_sanitize_history_rows` allowlist, and `content` is built only from templates (R0). Pinning tests use sentinel values.

**Brain-bound text, per path:**
- history = marker + template (FINDINGS: booleans, input column names and fixed codes, plus brain text);
- title and report = `brain_view` (the section choice carries no more than the FINDINGS);
- stopped records and edit-regenerate = the same builders;
- PDF/PPTX, dashboards and shares = rendered text that stays inside the client.

**Pre-existing channels — decisions F1–F3:**
- **Closed in session 1:** the `data_caveat` constant-value hints and dataset_profile min/max/top_values (B7.6), and quoted literals in `/v1/retry` `error_msg` together with the `NearZeroChartError` numbers (B7.7).
- **Kept by decision:** schema category lists for columns with ≤ 20 distinct values (`SCHEMA_VALUE_DENY_COLUMNS` is the tool for sensitive columns), and old history rows whose content holds summarize answers with values (left as they are).

---

## E. Backward compatibility and deployment order

**Deployment order:** brain first, then client. Client kill-switch: `BRAIN_CAPABILITIES=""`.

| Client \ Brain | Old brain | New brain |
|---|---|---|
| **Old client** | today | no `capabilities` ⇒ byte-identical prompts/responses; a null summarize preview (old client's dict-of-lists → None) gets the reading guide (a behaviour change, documented) |
| **New client** | `capabilities`/`findings` ignored (`body.get`); no `narrative` ⇒ describe/summarize fallback; summarize gets `preview: null` without a reading guide (the prose may invent a number, which is why the brain deploys first; the local `value_display` still shows the real value); no `diagnostic` ⇒ no phase 2 | full feature (phase 2 only once session 3 adds `two_phase_v1`) |

**Stored records:**
- All new fields are optional and additive. Records without them render exactly as today (the `renderAiRecord` old-record path; fixture `tests/fixtures/history_pre_narrative.jsonl`).
- No migration; DATA_ROOT is untouched.
- Downgrade (a new record read by an old client): `content` = marker + template is shown raw (accepted, decision F17).

**Executor/web image skew:**
- An older executor image without `pdc_diag` makes generated code raise NameError, which is retried. The `/healthz` handshake already warns on version skew.
- `_reconstruct` tolerates the new keys being absent.

---

## F. Decisions (2026-10-23) and remaining questions

| # | Question | Decision |
|---|---|---|
| F1 | Value hints in existing payloads | Scrub the `data_caveat` constant-value hints and dataset_profile min/max/top_values in session 1 (B7.6). Keep the schema category lists for ≤ 20 distinct values; `SCHEMA_VALUE_DENY_COLUMNS` stays the tool for sensitive columns. |
| F2 | `/v1/retry` `error_msg` | Scrub quoted literals (keeping input column and df names) and make `NearZeroChartError` a fixed sentence, in session 1 (B7.7). |
| F3 | Old history rows with values | Leave them as they are. |
| F4 | Phase-1 code in the phase-2 request | Yes, as one history-like row (code only, no values) (B9.2). |
| F5 | Phase-2 failure | Keep the streamed phase-2 visuals; drop the phase-2 narrative. |
| F6 | Phase 2 in the single flow | No. |
| F7 | shap | Keep the fallback; no new dependency. |
| F8 | pct input | A fraction, no heuristic. |
| F9 | Local fallback value display | Confirmed. |
| F10 | Reading guide for all null previews | Confirmed. |
| F11 | PROTOCOL copies | Synced now: the client copy = the brain copy (the brain had the newer answer-language text), DRAFT included. |
| F12 | Amounts ≥ 1e12 | `trln` added (A4). |
| F13 | FINDINGS keys | Keep the registry plus planner-literal keys as designed. The bit-budget analysis was removed from this spec. |
| F14 | ENUM | Added `data_gap`, `outlier_driven`, `seasonal_pattern`; `needs_phase2` is no longer a value (it stays the reserved boolean key). |
| F15 | Templates in Auto Analytics / report flows | Deferred. |
| F16 | Captions after Refresh / in dashboard tiles | Deferred. |
| F17 | Downgrade shows raw templates | Acceptable. |
| F18 | Default capabilities | `narrative_v1` and `diagnostics_v1` on by default; per-tenant switch later. `two_phase_v1` only with session 3. |
| F19 | Markdown injection via text VALUES | Escape `*`, `_` and a leading `-` (A2.2). |
| F20 | ru/uz locale number formatting | Decided 2026-10-25: `uz` and `ru` locales in `pdc_diag._LOCALES` (no-break-space thousands, comma decimal, mln/mlrd/trln and млн/млрд/трлн), chosen from the answer language (A4 rule 10). Month names stay in the file's own form. |
| F21 | `attribute_level` default | Keep variance. |
| F22 | "This month" timezone | Deferred; UTC for now. |
| F23 | Live SELECT aliases | Closed (`input_columns` holds registered names only). |
| F24 | History marker cap 400 chars | Confirmed. |
| F25 | Third prompts key `retry_narrative_system` | Confirmed. |
| F26 | Missing caption with a narrative | Keep A.5: a per-chart, value-free describe for that visual only. |

| F27 | Turn-time limits (session 3) | Keep: phase 2 is skipped when phase 1 took ≥ 120 s, and edit-regenerate never runs phase 2 (B9.1). |
| F28 | `two_phase_v1` gate | Accepted: diagnostics ship single-pass until session 3 adds `two_phase_v1` to the client default capabilities. |

No questions remain open for sessions 1 and 2.
