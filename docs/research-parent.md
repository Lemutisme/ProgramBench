# Full-revision research parent

The normal full AgentRevision loop uses its formally adopted incumbent as the next generator parent. A new study can opt into separate research inheritance with:

```json
"research_parent": {"protocol": "current-d-best-v1"}
```

The field is absent by default. It requires version 2, recursive full AgentRevision proposals, the existing closed D-only feedback protocol, and both formal adoption gates fixed to strictly more than +2 percentage points with all assigned scores valid. Existing studies are not converted or resumed under changed settings.

`I` is the formal incumbent. `R` is an isolated research parent. Initially they are the same complete revision. Proposals execute R's binary, use R's generator policy, and patch R's exact source; their parent and artifact receipts continue to use the existing native validation path.

After every proposal is sealed, the controller evaluates I and all completed new proposals on the current D tasks and repeat coordinates. If R differs from I it also evaluates a separately labelled `research-parent` subject. Old scores are never used as the current parent comparator. The seed is a campaign coordinate and ordering input, not a guarantee of deterministic provider sampling.

The next R is the complete valid subject with the largest task-equal D mean. Ties prefer the current R, then I, then the earliest predeclared proposal. A missing or failed coordinate cannot be dropped. When no subject has a complete D measurement, R stays unchanged and the decision records that no new subject was selected. This research choice does not claim an independently confirmed improvement.

The controller seals `research-decision.json` before any P work. Formal D/P selection remains the existing one-finalist process against I, with the original strict +2pp gates. Carried R is not a new proposal and cannot automatically enter the P contest. A P result cannot change the sealed research choice. Only a formally accepted new proposal updates I.

Persistence is explicit:

- `binding.json` retains the incoming formal binding and adds `research_binding`.
- Each evaluation records `subject_roles`; identical I/R uses one run with both roles.
- `research-decision.json` binds both incoming revisions, the sealed proposal barrier, exact current D specifications/results, selection and next R.
- `decision.json` retains `previous_binding`/`next_binding` for I, adds the corresponding research bindings and hashes the separate research decision.
- `completed.json` retains `final_binding` as I and adds `final_research_binding`.

Cached operations revalidate their specifications and native receipts as before. Research selection is additionally recomputed against its sealed D inputs. Feedback and the next round cannot consume an unclosed or conflicting research decision. Feedback identifies the carried research subject's exact revision, policy/runtime and roles; it exposes closed D evidence, with P results and decisions excluded.

The extra work is one D block per repeat whenever R differs from I. With D6, one repeat and two new proposals, the normal 18 solver/grader coordinates become 24. R = I adds none. Formal P work remains two subjects on the original P block when its gate opens. Every new instance retains an original six-hour deadline-only budget without cumulative turn, action, request or money limits. This implementation does not add a second D repeat or alter a running instance's deadline.

Controller tests use a deterministic backend and explicitly synthetic revision identities; they do not build a native runtime, call a model or run a ProgramBench grader. This support enables an experiment on cumulative research inheritance. Its existence does not establish higher scores or improved improver ability.
