# Codex Usage Analysis

**Obtain a cost estimation for other models based on your real codex usage**

A Python tool for auditing tokens reported in local Codex logs, simulating their
cost across multiple providers, and preparing a per-session dataset.
Codex sources are opened read-only. No API calls or keys are required.

## Installation

Python **3.9 or later**, with no external dependencies. Clone the repository and
open a terminal in its directory. The commands also work from another directory
when using the full path to the script.

```bash
sh run_analysis.sh
```

The launcher runs the analyzer first, then compares model costs only if the
analysis succeeds. It works from any directory when called with its full path.

The analysis searches for active and archived rollouts under `$CODEX_HOME`, or
`~/.codex` by default, then supplements the list using `state_5.sqlite` if present.
Results are written to **`outputs/`.

## Commands

```bash
# Run both steps with a custom output directory and analyzer options
sh run_analysis.sh outputs/weekdays --exclude-days sat sun

# Choose the logs and output directory
python3 src/analyze_codex_usage.py --codex-home /path/to/codex --output-dir outputs/run

# Add a rollout root directory without modifying the sources
python3 src/analyze_codex_usage.py --extra-root /path/to/archived-rollouts

# Exclude weekends from the audit
python3 src/analyze_codex_usage.py --exclude-days sat sun

# Reprocess the same file prefixes with SHA-256 verification
python3 src/analyze_codex_usage.py --manifest outputs/source_manifest.json --output-dir outputs/replay

# Compare costs using an existing audit
python3 src/compare_models.py --input outputs/token_usage.csv --output-dir outputs/comparison

# Check accounting rules against test data
python3 -m unittest discover -s src -v
```

`analyze_codex_usage.py` also accepts `--timezone` (Europe/Paris by default).
`compare_models.py` accepts `--pricing` and uses every input row and its recorded
date. It reports averages per active day and per calendar day across the input
period, including days with no events in the calendar-day average.

Rerunning in the same directory replaces the corresponding generated results.
Use a new `--output-dir` to preserve a previous analysis.

The analyzer includes all days by default. `--exclude-days` accepts space-separated
`mon tue wed thu fri sat sun` values (case-insensitive), evaluated in `--timezone`.
Excluded usage is removed from all audit token totals and recorded separately in
`summary.json` as `excluded_usage`; selected weekdays appear in `excluded_days`.
When replaying a manifest, pass `--exclude-days` again to apply the same filter.
The comparison script applies no additional weekday exclusions.

## Structure

```text
run_analysis.sh                Run extraction and cost comparison in sequence
src/analyze_codex_usage.py      Extraction, aggregation, validation
src/compare_models.py          Multi-model cost simulation
src/test_analyze_codex_usage.py Synthetic token counter tests
src/test_compare_models.py      Model comparison tests
comparison_pricing.json        Comparison rates, sources, and schedule
.github/workflows/tests.yml    GitHub Actions tests
outputs/                       Local results, never tracked by Git
```

The repository requires no package installation or remote service.
CI uses only synthetic tests, never personal logs.

## Token Accounting

`token_usage_record.payload.usage` describes a single response. The fields
`thread_token_usage`, `turn_token_usage`, and `token_count.info.total_token_usage`
are cumulative and must not be added together directly.

The tool reconstructs deltas from the `token_count` stream, ignores repeated
cumulative totals, and uses `last_token_usage` at the start or after a counter
decrease to avoid charging for inherited history again. Explicit responses and
equivalent events are matched once, within the same turn, using their exact
components. The two cumulative streams remain separate, particularly after
compaction. Fork and sub-agent identities are distinguished from the parent.

Cached tokens are included in input: `uncached = max(input - cached, 0)`.
Reasoning tokens are included in output and are not charged twice.
Standalone context counters without billable components are flagged and
excluded. Unsupported structures cause explicit failures.

Automatic checks cover relationships between components, response uniqueness,
CSV token totals, and SHA-256 integrity of the analyzed file prefixes. The
application can continue appending lines during analysis; the analyzed byte
range is bounded.

## Outputs

* `token_usage.csv`: reconstructed responses/units, identities, model/provider,
  tokens and source file/line provenance.
* `sessions.csv`: aggregation and initial router dataset.
* `daily_summary.csv`, `model_summary.csv`, `session_model_summary.csv`: breakdowns.
* `summary.json`, `validation.json`, `source_manifest.json`: totals and checks.
* `comparison_models.csv`, `comparison_daily.csv`, `comparison_summary.json`:
  comparative estimates and daily averages.
* `comparison_models.md`: a concise table sorted from least to most expensive,
  with daily cost per active day and monthly cost projected over 30 active days,
  in USD, excluding the optional cache-write surcharge. Generated by
  `compare_models.py` in the selected output directory.

`compare_models.py` also prints the sorted cost table and output file locations
in the console. Full JSON details are saved in `comparison_summary.json`.

Quality, human acceptance, retry, and escalation fields remain NULL when they
cannot be established. A completed turn does not prove that a task succeeded.
A session can contain multiple turns and models. Its observed duration includes
pauses; it does not measure model latency.

## Models Included in Cost Estimates

By default, `compare_models.py` estimates costs for the eleven entries in
`comparison_pricing.json`. The bundled snapshot is dated **2026-09-13**,
with GLM-5.3, GLM-5.3-Flash, Qwen3.7 Plus, and Qwen3.5 Flash pricing
checked on **2026-09-14**:

| Configuration identifier | Model used for the estimate in this snapshot |
| --- | --- |
| `glm-5.3-flash` | GLM-5.3-Flash |
| `glm-5.3` | GLM-5.3 |
| `deepseek-v4-flash` | DeepSeek V4.1 Flash (legacy alias) |
| `deepseek-v4-pro` | DeepSeek V4 Pro 0813 |
| `qwen3.7-plus` | Qwen3.7 Plus — Frankfurt/global, implicit cache |
| `qwen3.5-flash` | Qwen3.5 Flash — Frankfurt/global, explicit cache |
| `gemini-flash` | Gemini 3.8 Flash |
| `gemini-pro` | Gemini 3.1 Pro Preview |
| `openai-gpt-5.6` | GPT-5.6 Sol |
| `openai-gpt-6-astra` | GPT-6 Astra |
| `mistral-medium-3.5` | Mistral Medium 3.5 |

The analyzer extracts and validates token usage without calculating costs.
`compare_models.py` handles all pricing, including DeepSeek peak/off-peak
schedules, and accepts `--pricing` to use a different pricing configuration.
These estimates apply configured rates to the observed Codex token workload;
they do not run or benchmark the listed models.

## Pricing and Limitations

All prices are centralized in `comparison_pricing.json`, in USD per million tokens.
Comparison rates are a **dated snapshot**, with links to official sources:
verify them before using them for budgeting. Generic names and aliases are
clarified in `resolved`.

Simulated costs preserve the token volumes and cache usage observed in Codex.
A different tokenizer, agent strategy, or success rate would change actual
consumption. Unobservable additional costs (cache storage, tools, taxes) are not
invented. The OpenAI cache write surcharge is presented as a separate scenario.
Qwen3.5 Flash assumes explicit cache reads; cache creation and storage fees
are excluded. Its three rate tiers change above 128K and 256K input tokens.
Context-size pricing tiers are applied per call. The DeepSeek schedule is applied
to timestamps as a simulation using published rates, not as a reconstruction of
past invoices.

Missing files, events that were not persisted, and remote sessions remain outside
the analysis coverage. The totals do not represent Codex subscription quotas.
