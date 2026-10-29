# Acquisition naming contract

**The YouTube video ID is the immutable primary identity.** The downloader keeps each acquisition in an ID directory with a generic filename, for example:

```text
state/acquisitions/MbO7y_90zUI/media.mkv
```

This layout is intentional. Acquisition paths never depend on the current title, and YouTube title changes must not rename, move, or otherwise alter acquired media for presentation. The inventory retains initial and latest observed titles and distinct metadata observations. Verified source publication/release dates are separate metadata fields; dates embedded in titles are not authoritative.

`state/acquisitions/` is an internal verified acquisition store, **not the final human-readable archive**. Stable paths keep SQLite receipts, manifests, hashes, retries and duplicate checks associated with the original video ID.

A future integration with `dls-archiver` may publish date-first descriptive names such as:

```text
YYYY-MM-DD - Show Name [YouTubeID].mkv
```

That example is illustrative. Final archive naming belongs to the Archiver and is not finalized. The downloader must preserve its acquisition paths independently of any future archive presentation scheme. A future `dls-indexer` should use stable video IDs and metadata rather than interpreting these generic acquisition filenames. No Archiver or Indexer integration is implemented by this contract.
