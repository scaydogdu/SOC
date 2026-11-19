# Brief: fix the spend guard, then close M2 with live runs

**From:** Claude (reviewer) · **For:** Codex · **Date:** 18 November 2026
**State:** `64e276b`. M2 passes 13 of its 14 scripted scenarios offline. It stays open until one live episode completes in every SOC condition.

## 0. Where things stand

- **Provider keys are configured.** Can stored them in the macOS Keychain. `~/.zshrc` exports `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` into every new shell. If your session can't see them, start it from a fresh Terminal shell. Never print, log, cache or commit a key.
- **The spend guard blocks live M2.** In the campaign `~/Library/Application Support/SOC/campaigns/m1-2026-11-18/`:

  | | Amount |
  |---|---|
  | Cap, stored in `spend.sqlite` | USD 40 |
  | Settled spend, 36 calls | USD 0.12 |
  | Still reserved, 5 Gemini calls with unknown usage | USD 34.96 |

  The guard reserves `max_input_tokens × input price + max_output_tokens × output price`, at the model's configured maximum.

  | Model | Reserved per call |
  |---|---|
  | Gemini 4 Argon | USD 26.40 |
  | GPT-6 Astra | about USD 31.80 |
  | Claude Opus 5.5 | about USD 7 |

  Under a USD 40 cap, at most one such call can be in flight, so triads cannot run. The cap is also pinned in the database: changing `spend_cap_usd` in `config/m1-live.yaml` makes the guard refuse to start.

Do sections 1–3 before any further live call.

## 1. Reserve what a request can actually cost

1. **An explicit output limit on every call.**
   - Every adapter sends an output limit on every request:
     - Anthropic `max_tokens`;
     - OpenAI `max_output_tokens`;
     - Gemini `generationConfig.maxOutputTokens`.
   - Take the value from the experiment or probe configuration, never from the model's maximum.
   - Remove the `caps_billed_output` special case once every route enforces its limit.
2. **Reasoning tokens.** For each provider, check in current documentation whether thinking or reasoning tokens count inside that output limit or are billed beyond it.
   - Where they can exceed it, set the provider's thinking budget or level so the total billed output is bounded.
   - Otherwise reserve for the larger amount.
   - Record each finding with its documentation link in the status file. Do not guess.
3. **A bounded input reservation.**
   - Reserve input from the actual request. Use a deterministic upper bound: the UTF-8 byte length of the full serialized request, including messages, schema and system text. Tokens never outnumber bytes for these tokenizers.
   - Test the bound against the 36 settled calls already in the campaign cache. Every reported `input_tokens` must be ≤ its bound. If any exceeds it, stop and report.
   - A provider token-counting endpoint may tighten the bound later, but not in this change.
4. **Formula.**
   - `reserve = bound_input × input price + output_limit × output price`, plus the reasoning allowance from step 2 where one applies.
   - The model's `max_*` values stay as validation ceilings only: a request above them is refused.
   - Settlement is unchanged: provider-reported usage settles the charge, and usage above the reserved bound still trips `bound_exceeded` and stops the campaign.
5. **Tests.**
   - A typical toy request reserves less than USD 0.10 on every roster model.
   - A request whose bound exceeds the model ceiling is refused.
   - Usage above the reservation still trips `bound_exceeded`.
   - Three concurrent triad calls fit under a USD 40 cap.

## 2. Operator tools for the spend ledger

Add these as subcommands of `python -m soc.orchestrator.live`. Each acts only on an explicit campaign directory.

- **`spend status`** prints:
  - the cap;
  - settled spend;
  - each open reservation with its ID, model, run, call ID, outcome and amount.
- **`spend settle --reservation ID --usd AMOUNT --reason TEXT --approved-by NAME`**
  - Settles one open reservation at an amount a person confirmed from the provider's billing console. `0` is allowed for an unbilled call.
  - Refuses a reservation that isn't open.
  - Appends the action to a new append-only `operator_actions` table, with the time, the before and after amounts, the reason and the approver.
- **`spend set-cap --usd AMOUNT --reason TEXT --approved-by NAME`**
  - Changes the stored cap. Refuses a cap below current settled plus reserved spend.
  - Logs the action the same way.
  - After this, starting the guard with a config value that differs from the stored cap is still an error. The message must say to use `set-cap`.

Rules:

- The guard must never release, settle or raise anything on its own.
- These commands are run by Can. Do not run `settle` or `set-cap` on the real campaign yourself. Test them on copies.
- Document all three in `README.md`.

## 3. Hand the stuck reservations to Can

After sections 1 and 2 pass `./scripts/check`:

1. Run `spend status` on the real campaign and paste the open reservations into the status file.
2. Can checks the Google billing console for those five calls. They were a rate limit and server errors, which may not have been billed, but only the console says. Can then settles each one with `spend settle`.

## 4. Re-probe

```sh
uv run --locked python -m soc.orchestrator.live probe --output "$HOME/Library/Application Support/SOC/campaigns/m1-2026-11-18"
```

- **Goal:** all 18 model and effort combinations pass. Gemini 4 Argon and Gemini 3.8 Flash are the open ones.
- **If a Gemini model still fails after the provider's documented retry guidance:**
  - Report the error codes.
  - Do **not** swap the roster. Changing a triad family's model is Can's decision.
  - The SOC triad needs three distinct families, so no SOC condition runs without a passing Gemini model at the required tier.

## 5. Prepare the live M2 conditions; don't launch them

1. **List every SOC condition** that `PROJECT_PLAN.md` defines for the M2 live check. Cover each resolution mode (`cross_examination`, `open_discussion`, `principal_only`) and each effort level the plan uses for SOC. Put the list in the status file and wait for Can to confirm it.
2. **Write one condition YAML per condition** under `config/m2-live/`, using the toy episode and `run.max_in_flight: 1`.
3. **Write the principal key** for the toy episode outside the repository, for example `~/Library/Application Support/SOC/principal/toy.yaml`. Check that no worker input can reach it.
4. **Estimate cost before spending.**
   - Run each condition with scripted providers to count calls and tokens.
   - Price the counts with the new reservation formula.
   - Report per condition and in total: expected spend, worst-case reserved spend, and peak concurrent reservation.
   - Can approves the budget, and raises the cap with `set-cap` if needed, before anything live runs.

## 6. Live runs (after Can approves the list and budget)

- Run each condition once with `soc.orchestrator.live soc`, in the existing campaign, with a fresh run directory per condition.
- Can may prefer to run these in Terminal. Put the exact commands in the status file either way.
- After each run:
  - replay it cache-only with `soc-replay` into a new directory and confirm byte-identical outputs;
  - record the calls, tokens, settled spend and gate results.
- If a run stops on budget, a rate limit or a provider error, record it and stop. Do not retry in a loop.

## 7. Then

- **Timeline export.** Build it as specified in `CODEX_BRIEF_TIMELINE.md`, and generate a timeline page for each live M2 run under `docs/examples/`.
- **SHACL fixtures.** Add the missing invalid fixtures: `DecisionSubjectShape`, `BriefCitationShape`, and the `authorityClass` constraint on `CommitmentShape` have none. Each invalid fixture must fail only its own shape.

## 8. Hand back

Update `M2_STATUS.md` with:

- the reservation formula and the reasoning-token findings with their links;
- the bound check against the 36 cached calls;
- the new commands and their tests;
- `spend status` before and after Can's settlement;
- probe results;
- the condition list with Can's confirmation;
- the cost estimate;
- per-run live results and their replay checks;
- the `./scripts/check` output.

Commit in logical steps. Put anything this brief leaves to Can in `OPEN_QUESTIONS.md`.
