# Provider Smoke Test Runbook

Opt-in smoke tests that send **one tiny, fixed, synthetic request** to each real
external provider the platform can use: OpenAI (LLM), AssemblyAI (STT),
ElevenLabs (TTS) and SMTP (email). They answer one question: *do the configured
credentials, model/voice identifiers and network path work right now?* They
are not quality, load or end-to-end tests (for the live interview E2E see
`docs/P0_6_LIVE_AI_VERIFICATION.md`).

| File | Purpose |
| --- | --- |
| `tests/live/test_provider_smoke.py` | The four smoke tests (marker `live`). |
| `tests/live/smoke_harness.py` | Opt-in gating, sanitizer, result record. No network I/O. |
| `tests/unit/test_provider_smoke_harness.py` | Offline checks of the harness (run in CI, `security-regression` group). |

By default every smoke test **skips**. `pytest tests/live` must report
`4 skipped`. Nothing in this runbook runs in CI automatically; the existing
`live-provider` workflow job does not run `tests/live`.

## 1. Safety design

* **Two opt-ins plus full configuration.** A test runs only when
  `RUN_LIVE_PROVIDER_SMOKE=1` **and** its provider opt-in (`LIVE_SMOKE_<PROVIDER>=1`)
  are set **and** every required variable below is set to a non-placeholder
  value. Any partial configuration skips with a message that names the missing
  variables (never their values).
* **Fixed, synthetic input.** No candidate data, no database rows, no files
  from `storage/`.
* **No retries.** Every provider is constructed with `max_retries=0`; the
  OpenAI structured call is therefore a single HTTP request.
* **Hard timeouts.** pytest-timeout per test (60 s; AssemblyAI 90 s) plus
  provider timeouts (HTTP 15-20 s, AssemblyAI transcript deadline 45 s, SMTP 10 s).
* **Credential hygiene.** Variables are read from the shell environment only.
  Each test emits one JSON record through a sanitizer, then re-reads all
  captured stdout/stderr and log output and **fails** if any configured
  secret value appears in it. Failure messages contain only an error category
  (exception type, fixed provider error code, HTTP/SMTP status class), never
  exception messages or response bodies.

## 2. Environment variables

Export these in the shell that runs pytest. Do **not** put the
`RUN_LIVE_PROVIDER_SMOKE` / `LIVE_SMOKE_*` variables into `.env`:
`config/settings.py` rejects unknown keys in `.env`, and the smoke tests read
`os.environ`, not `.env`.

Always required by the test harness (`tests/conftest.py`), unrelated to providers:

```
ENVIRONMENT=test
DATABASE_URL=sqlite:///./storage/test_live_smoke.db   # never opened by the smoke tests
ENABLE_DEV_ADMIN_BOOTSTRAP=false
```

| Provider | Opt-in | Required | Optional | Max cost per run |
| --- | --- | --- | --- | --- |
| OpenAI | `LIVE_SMOKE_OPENAI=1` | `OPENAI_API_KEY`, `OPENAI_MODEL` | `OPENAI_BASE_URL` | < USD 0.01 (one request, ~150 input tokens, max 16 output tokens) |
| AssemblyAI | `LIVE_SMOKE_ASSEMBLYAI=1` | `ASSEMBLY_AI_API_KEY` | `STT_MODEL` (default `best` = account default), `STT_LANGUAGE` (default `en-US`), `LIVE_SMOKE_ASSEMBLYAI_KEEP_TRANSCRIPT=1` (skip auto-delete) | < USD 0.01 (1 s of audio) |
| ElevenLabs | `LIVE_SMOKE_ELEVENLABS=1` | `ELEVEN_LABS_API_KEY`, `ELEVEN_LABS_VOICE_ID` (a real voice id, not `default`), `TTS_MODEL` (not `local-fake`) | - | < USD 0.01 (21 characters: "Provider smoke check.") |
| SMTP | `LIVE_SMOKE_SMTP=1` | `EMAIL_SMTP_HOST`, `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD`, `EMAIL_SMTP_FROM`, `LIVE_SMOKE_SMTP_TEST_RECIPIENT` | `EMAIL_SMTP_PORT` (default 587; STARTTLS is mandatory, implicit-TLS port 465 is not supported) | No provider spend; 1 message |

Global: `RUN_LIVE_PROVIDER_SMOKE=1`. Optional: `LIVE_SMOKE_RESULT_FILE=<path>`
appends each JSON record to that file (JSON Lines).

The cost bounds are upper bounds derived from the fixed inputs at public list
prices; they are not enforced by the providers. The provider-side spend cap
(section 3) is the real control.

## 3. Authorization checklist (complete before every run)

- [ ] **Approver** - the project owner (or their delegate) approves the run in
      writing (ticket/PR comment), naming which providers will be called.
- [ ] **Test accounts** - each key belongs to a dedicated non-production
      account/project (OpenAI project, AssemblyAI account, ElevenLabs workspace,
      SMTP sandbox or test mailbox) that never processes candidate data.
- [ ] **Spend cap** - a provider-side hard limit is set: OpenAI project budget
      at most USD 5 (matches the `confirm_live_ai_spend_cap` input of the CI
      `live-provider` job); AssemblyAI and ElevenLabs on a capped/prepaid test
      plan.
- [ ] **Test recipient** - `LIVE_SMOKE_SMTP_TEST_RECIPIENT` is a mailbox the
      operator controls (or a provider sandbox inbox). Never a candidate,
      customer or distribution list.
- [ ] **Key handling** - keys come from a secret manager into the shell; they
      are not typed into files, tickets, chat or command history
      (e.g. `read -s` / a password-manager CLI). Rotate any key that appears in
      output.
- [ ] **Scope** - only the providers you opted into will be called; leave the
      others' `LIVE_SMOKE_*` unset.

## 4. Commands

Bash (Linux/macOS/Git Bash), one provider at a time:

```bash
export ENVIRONMENT=test ENABLE_DEV_ADMIN_BOOTSTRAP=false
export DATABASE_URL=sqlite:///./storage/test_live_smoke.db
export RUN_LIVE_PROVIDER_SMOKE=1
export LIVE_SMOKE_RESULT_FILE=./storage/test_live_smoke_results.jsonl

# OpenAI
export LIVE_SMOKE_OPENAI=1 OPENAI_MODEL=<approved-model-id>
read -rs OPENAI_API_KEY && export OPENAI_API_KEY
python -m pytest tests/live/test_provider_smoke.py -k openai -m live -rA

# AssemblyAI
export LIVE_SMOKE_ASSEMBLYAI=1
read -rs ASSEMBLY_AI_API_KEY && export ASSEMBLY_AI_API_KEY
python -m pytest tests/live/test_provider_smoke.py -k assemblyai -m live -rA

# ElevenLabs
export LIVE_SMOKE_ELEVENLABS=1 ELEVEN_LABS_VOICE_ID=<voice-id> TTS_MODEL=<model-id>
read -rs ELEVEN_LABS_API_KEY && export ELEVEN_LABS_API_KEY
python -m pytest tests/live/test_provider_smoke.py -k elevenlabs -m live -rA

# SMTP
export LIVE_SMOKE_SMTP=1 EMAIL_SMTP_HOST=<host> EMAIL_SMTP_PORT=587
export EMAIL_SMTP_USERNAME=<user> EMAIL_SMTP_FROM=<from-address>
export LIVE_SMOKE_SMTP_TEST_RECIPIENT=<operator-test-mailbox>
read -rs EMAIL_SMTP_PASSWORD && export EMAIL_SMTP_PASSWORD
python -m pytest tests/live/test_provider_smoke.py -k smtp -m live -rA
```

PowerShell equivalent for a secret: `$env:OPENAI_API_KEY = Read-Host -MaskInput`
(PowerShell 7) and `$env:LIVE_SMOKE_OPENAI = "1"`.

Default (no opt-in) check, safe to run anywhere:

```bash
ENVIRONMENT=test DATABASE_URL=sqlite:///./storage/test_live_smoke.db \
  python -m pytest tests/live -q -rs        # expect: 4 skipped
```

Afterwards: `unset OPENAI_API_KEY ASSEMBLY_AI_API_KEY ELEVEN_LABS_API_KEY EMAIL_SMTP_PASSWORD`
(or close the shell) and delete `storage/test_live_smoke*.{db,jsonl}`.

## 5. Expected output

One line per test on stdout (shown with `-rA`), plus one JSON line in
`LIVE_SMOKE_RESULT_FILE` if set:

```
LIVE_SMOKE_RESULT {"details": {"model": "<model>", "ok": true, "schema_validated": true}, "error_category": null, "latency_ms": 812.4, "max_cost_usd": 0.01, "provider": "openai", "recorded_at": "2026-11-24T10:00:00+00:00", "success": true}
```

Record keys (always exactly these): `provider`, `success`, `latency_ms`,
`error_category`, `max_cost_usd`, `details`, `recorded_at`. Provider details:

* openai: `model`, `schema_validated`, `ok`
* assemblyai: `model`, `audio_seconds`, `final_events`, `transcript_chars`
  (the synthetic tone has no speech, so `0` is normal), `transcript_id`,
  `transcript_deleted`, `delete_status_class`
* elevenlabs: `model`, `input_characters`, `audio_bytes`, `chunks`
* smtp: `outcome`, `error_class`, `recipient_domain` (domain only)

A passing test prints `PASSED`; a failure prints only
`<provider> smoke test failed: <error_category>`.

## 6. Interpreting failures

| `error_category` | Meaning | Action |
| --- | --- | --- |
| `LLMConfigurationError` | OpenAI 401/403: key invalid, revoked, or project lacks model access | Check key/project; do not retry repeatedly |
| `LLMRateLimitError` | OpenAI 429: rate limit **or exhausted budget/spend cap** | Check usage page and cap |
| `LLMProviderError` | OpenAI 5xx, network failure, or non-chat response | Retry later once; check status page |
| `HTTPStatusError:4xx` (OpenAI) | Other 4xx, commonly an unknown `OPENAI_MODEL` or a model that rejects `max_tokens`/`temperature` | Verify the model id; the app uses the same request shape |
| `LLMInvalidOutputError` | Model answered but not with `{"ok": true}` JSON | Model is reachable; structured-output reliability issue for that model |
| `LLMTimeoutError` | No response within 20 s | Network/egress or provider latency |
| `Timeout >60.0s` (no record) | pytest-timeout hard limit hit; the run is aborted before a record is emitted | Same as above; never raise the limit to "make it pass" |
| `STTProviderError:STT_AUTH_ERROR` | AssemblyAI 401/403 | Key |
| `STTProviderError:STT_RATE_LIMITED` | AssemblyAI 429 | Account limits |
| `STTProviderError:STT_ERROR` | 4xx/5xx/invalid response, or transcription status `error` | Check `transcript_id` in the AssemblyAI dashboard; an unsupported `STT_MODEL` shows here |
| `STTProviderError:STT_TIMEOUT` | Not completed within the 45 s deadline | Provider queueing; retry later once |
| `TTSProviderError:TTS_AUTH_ERROR` | ElevenLabs 401/403 | Key/workspace |
| `TTSProviderError:TTS_RATE_LIMITED` | ElevenLabs 429 or quota exhausted | Quota |
| `TTSProviderError:TTS_ERROR` | Other 4xx (commonly an invalid `ELEVEN_LABS_VOICE_ID` or `TTS_MODEL`) or 5xx | Verify ids |
| `EmptyAudio` | 2xx with no audio bytes | Provider anomaly |
| `smtp:definitively_rejected:authentication_rejected` | SMTP login refused | Credentials / app-password policy |
| `smtp:retryable_not_accepted:connection_failure` | Cannot connect, STARTTLS failed, timeout | Host/port/egress firewall; port 465 is unsupported |
| `smtp:*:recipient_refused` / `sender_refused*` | Server refused the recipient or From address | Sender domain authorization (SPF/DKIM), recipient validity |
| `smtp:unknown:transport_outcome_unknown` | Failed after DATA; the message **may** have been delivered | Check the test mailbox before re-running |
| `Failed: A credential value appeared...` | A secret was found in captured output | Treat the key as exposed: rotate it, then investigate |

A skip is not a pass. `SKIPPED ... missing: X` means the run was not authorized
or not fully configured.

## 7. Cleanup

* **AssemblyAI** - the test deletes its transcript (`DELETE /v2/transcript/{id}`)
  unless `LIVE_SMOKE_ASSEMBLYAI_KEEP_TRANSCRIPT=1`. If `transcript_deleted` is
  `false`, delete it manually with the recorded `transcript_id`:
  `curl -X DELETE -H "authorization: $ASSEMBLY_AI_API_KEY" https://api.assemblyai.com/v2/transcript/<transcript_id>`.
  The uploaded synthetic audio file has no delete endpoint; it contains only a
  generated 440 Hz tone and expires under AssemblyAI's retention policy.
* **ElevenLabs** - streaming TTS keeps no project; check the workspace history
  and delete the generated item if history is enabled for the test workspace.
* **OpenAI** - nothing to clean up (no files, assistants or fine-tunes created).
* **SMTP** - delete the smoke message from the test mailbox.
* **Local** - remove `storage/test_live_smoke*` files; unset the key variables.
