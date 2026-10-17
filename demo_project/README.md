# Ledgerlane demo app

This is a fictional app for Twin's meeting demos. Its web route logs users in
with a signed JWT. Billing reads Postgres records and emails invoice notices.
All users, companies, and history in this directory are invented.

Run the small offline check from this directory with
`python -m unittest discover -s tests`. No database or credentials are needed.
`fake_history.sh` copies this directory to a temporary Git repository and
creates three fictional-author commits there for code ownership demos.
