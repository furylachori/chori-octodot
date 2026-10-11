# Cloud Build checks

`cloudbuild.yaml` is the fast PR profile. It uses Python 3.13.7 and Git from the pinned Debian Bookworm container, checks syntax and CLI help/version, then runs `ParserTests`, `OutputTests`, and `ArchitectureTests`. It does not run the transport, API simulation, artifact, or Git fixture suites.

`cloudbuild-offline.yaml` is the fuller provider-free profile intended for a separately configured 06:00 and 18:00 America/Costa_Rica run. It runs the complete `test_octodot` unittest suite, including mocked REST, pagination, concurrency, artifact, and temporary local-Git cases. The global socket guard in the suite rejects network attempts. No Jules API key, Google Cloud credential, test secret, provider call, artifact upload, or deployment is configured.

The existing [GitHub Actions offline matrix](../.github/workflows/offline.yml) remains unchanged. It still covers Python 3.10–3.13 on Ubuntu and macOS; these Cloud Build profiles use one pinned Linux runtime. Cloud Build installs Git because the architecture and Git fixture tests require it. Its source snapshot is indexed locally so the existing exact tracked-file allowlist test also works when `.git` metadata is absent.

Both configs use bounded build and step timeouts and send logs only to Cloud Logging. A clean build installs Git from the pinned Bookworm distribution image; the unittest suite's network guard rejects test-time network calls. The repository files select no service account and create no trigger or schedule.

## Cloud Build identities

When setting up a trigger externally, configure the existing `ci-pr-smoke` account as the build execution identity after checking it against the actual build path. The identity that invokes the trigger is a separate principal and must be authorized separately for that trigger. The build account's Logs Writer and custom `cloudbuild.builds.create` permission do not establish the trigger invoker's access. Do not grant broad project roles or unconditional self-impersonation. See Google's [Cloud Build service account guidance](https://docs.cloud.google.com/build/docs/cloud-build-service-account) and [user-managed service account guidance](https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts).

The external trigger and schedule setup is intended for project `project-33a976dd-e360-4c43-8d9`, region `northamerica-south1`, and connection `mex`. No Cloud Build project settings, IAM, APIs, triggers, or schedules are changed here.
