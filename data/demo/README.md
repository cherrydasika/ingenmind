# Demo knowledge base

Fictional documents for Lakeshore Rail, an invented train operator in the
invented Merrow Valley. They exist so a fresh install has something to
search and answer from; none of it describes a real company or real travel
rules. `python -m seed_demo` ingests every other `.md` file here (the first
`# ` line is the document title). Each file marks itself as fictional in an
HTML comment, which is not ingested: in the searchable text, the notice made
the agents answer "this place does not exist" instead of answering.
