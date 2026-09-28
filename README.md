# Bistar Tender Watch

Daily scan of news feeds, procurement portals and World Bank notices for tenders, EOIs and bids
matching Bistar's sectors. New matches are emailed as one digest.

## Deploy (10 minutes)
1. Create a **private** GitHub repo and upload all files (keep the `.github` folder).
2. Repo > Settings > Secrets and variables > Actions > add:
   - `SMTP_USER` = emmanuel.iwere@bistarng.com
   - `SMTP_PASS` = mailbox app password (Google Workspace: Account > Security > App passwords)
   - `MAIL_TO` = emmanuel.iwere@bistarng.com
   - `SMTP_HOST` / `SMTP_PORT` = only if not Google (defaults: smtp.gmail.com / 465)
3. Actions tab > Tender Watch > Run workflow. First run may send a large digest.

## Local test
    pip install -r requirements.txt
    python tender_watch.py --dry          # show matches
    SMTP_USER=... SMTP_PASS=... python tender_watch.py --test-email

## Tune
Edit the term lists, `SEARCH_QUERIES` and `PAGES` at the top of `tender_watch.py`;
add Google Alerts RSS URLs to `feeds.txt`.
