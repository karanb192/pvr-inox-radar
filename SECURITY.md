# Security

## Reporting

Email karan@karanbansal.in with a description and reproduction steps.
You will get a reply within a few days. Please do not open a public issue
for anything sensitive.

## Scope notes

- This tool stores no credentials and needs none: the upstream API is
  called with a deliberately blank bearer token, exactly as the public
  PVR INOX website does.
- Everything runs locally on the user's machine. There is no server, no
  telemetry, and no data leaves the machine except the API calls the tool
  documents.
- The generated map HTML embeds only data returned by the API for the
  user's own query.

If you believe the tool can be made to misbehave against the upstream API
(amplification, cache poisoning, anything that could harm the service),
that is in scope and I want to hear about it.
