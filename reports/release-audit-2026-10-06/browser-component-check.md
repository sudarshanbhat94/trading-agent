# Browser observation — isolated component, 6 October 2026

The current `renderIdeaTracking`, `ideaAssessmentHtml` and `ideaShowAssessmentHistory` functions from the assembled SPA were rendered with synthetic data on a read-only localhost fixture. No production session, broker credentials or trading controls were used. This is a component check, not a full production journey.

- Native summary expansion revealed the original plan and Decision history.
- “Load dated checks” retrieved a dated rejected rebound predicate; the visible label says these are research observations, not orders or fills.
- `<img src=x onerror=alert(1)>` appeared as text; no image element was created.
- At 390 × 844, viewport, document and body width were all 390; the expanded card and controls remained readable. The temporary viewport override was reset.
- Authentication, watchlisting, approved buying, protection, subscriptions, all mobile breakpoints and WCAG acceptance remain unverified in a production browser. No evidence here changes model approval.
