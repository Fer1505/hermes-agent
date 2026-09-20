# Public business contact references

Public business telephone details can be displayed through scoped references
without exempting arbitrary numbers from ordinary redaction. The current
retrieval path supports two kinds of reviewed authority in protected profile
configuration:

- `grants`: an exact telephone already established by a reviewed business record.
- `source_grants`: an official business contact page and its telephone field,
  allowing the telephone value to be discovered at retrieval time.

Both lists default to empty. Page instructions, search snippets, citations and
model-generated metadata cannot create either kind of authority. A source grant
does not establish the official business identity by itself: the operator or
trusted business-record workflow must establish that relationship first.

## Source-scoped lookup

The following reserved example authorizes one source and contact label for a
dedicated task/conversation. Replace its fields with the reviewed record; the
example itself is not an authorization.

```yaml
security:
  public_contacts:
    max_age_seconds: 3600
    source_grants:
      - id: example-business-main-phone
        business: Example Business
        source_url: https://business.example/contact
        label: Main phone
        session_id: dedicated-conversation-id
        task: Find this business's published main telephone
        expires_at: "2026-10-01T00:00:00Z"
```

The retrieved page must contain a line such as `Main phone: +1 (202) 555-0142`.
The exact reviewed label is matched case-insensitively, with plain or bold
Markdown labels and optional list markers. Values support the existing
international/extension-bearing display forms. Bold values and Markdown links
to global `tel:` numbers are supported; source offsets identify the actual
published value. Conflicting numeric link labels, local telephone URIs without
a global context, URI parameters and arbitrary URL links do not qualify through
the link path. This is a bounded extractor, not a complete telephone parser or
a check that the number is allocated, reachable or answered.

Only the successful, policy-checked `web_extract` result can issue a reference.
Requested and reported source URLs must both equal the reviewed HTTPS page.
Redirects to other sources do not inherit authority. The current dispatcher
session and tool correlation are required. Each source grant yields at most
eight distinct labeled contacts per extraction. A provider-supplied reference
is discarded before trusted issuance.

The model receives an opaque reference to copy into its reply. At a supported
display boundary, the reference becomes the business name, published telephone,
source link and retrieval timestamp after ordinary secret filtering. A missing,
expired, revoked or differently scoped reference remains unavailable. Cached
retrieval keeps the original cache timestamp.

## Evidence and recovery

Exact-phone grants retain their version 1 receipt format and digest. Source
grants produce version 2 receipts containing the discovered telephone, its
source span/digest, the reviewed label, source grant digest, profile, session,
task and dispatcher correlation. These are owner-only files under the existing
protected `public-contact-receipts` directory. They are evidence records, not
ordinary logs or model-created authorization.

Current source authority is checked for every new display. Separately stored
historical display evidence preserves what was verified when its assistant row
was saved, without reviving a revoked grant. Existing quick/pre-update recovery
copies both receipt versions and historical evidence with the conversation
database; routine code rollback does not rewind application data.

## Remaining workflow work

This configuration-backed source lookup removes the requirement to know the
telephone before retrieval. It does not complete automatic official-business
discovery or the trusted user-request-to-source authorization path. It also does
not certify every streaming/CLI/API/artifact display surface, compression
lineage, full archive lifecycle, live delivery or end-user acceptance. The full
business-contact workflow remains open until those paths are implemented and
verified.

## Format references

[RFC 3966](https://datatracker.ietf.org/doc/html/rfc3966) defines telephone URIs
and their visual separators; parsing one does not prove that a business owns
the number. [Schema.org telephone](https://schema.org/telephone) can describe
people as well as organizations, so the presence of that property alone is not
used to authorize business-contact display.
