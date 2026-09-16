# References folder

This is where the 36 reference PDFs for the methodology paper go.
Nothing is here yet — see `DOWNLOAD_MANIFEST.md` for the shopping
list with URLs, filenames, and access notes.

Naming convention across all files:

    NN_FirstAuthor-YYYY_short-topic.pdf

Where `NN` matches the reference number in `METHODOLOGY.md` § 13.
That way, when a co-author writes `[Ref 15]` in the paper draft, a
`ls | sort` in this folder immediately shows which PDF they mean.

Progress markers:

- 0 / 32 downloaded
- (32 target because 4 of the 36 references are software repos /
  ebooks that are not proper PDF papers)

When a file is added to this folder, tick it off in
`DOWNLOAD_MANIFEST.md` by prefixing its entry with `[x]`.

This folder is inside `data/`-equivalent territory in terms of what
we share — do not commit PDFs to git. `.gitignore` covers it:
`references/*.pdf` is already excluded, or add that line if not.
