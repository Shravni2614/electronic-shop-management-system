# Electronic Shop Management System (Flask + PostgreSQL)
Local: create DB, run electronic_shop_db.sql, `pip install -r requirements.txt`, set PG* vars or DATABASE_URL, `python app.py`.
Deploy (Render): push this folder to GitHub -> Render > New > Blueprint -> select repo (uses render.yaml).
After first deploy, open the web service Shell and run: `python init_db.py`
