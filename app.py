from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3, os, zipfile, csv, io
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "gadgetbay_fixed.db")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024
CORS(app)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(sku TEXT PRIMARY KEY, product TEXT, category TEXT, cost_price REAL, selling_price REAL, supplier TEXT);
CREATE TABLE IF NOT EXISTS inventory(sku TEXT PRIMARY KEY, new_stock INTEGER, avg_daily_demand REAL);
CREATE TABLE IF NOT EXISTS vendor_terms(supplier TEXT, sku TEXT, rtv_allowed TEXT, credit_pct REAL, window_days INTEGER, PRIMARY KEY(supplier,sku));
CREATE TABLE IF NOT EXISTS returns(return_id TEXT PRIMARY KEY, date_received TEXT, sku TEXT, supplier_batch TEXT, reason_code TEXT, condition_grade TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, sku TEXT, qty_sold REAL, selling_price REAL);
CREATE TABLE IF NOT EXISTS refurbishment(category TEXT PRIMARY KEY, refurb_cost REAL, days_required INTEGER, resale_pct_of_new REAL);
CREATE TABLE IF NOT EXISTS liquidation(category TEXT, condition_grade TEXT, recovery_pct_of_cost REAL, PRIMARY KEY(category,condition_grade));
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT NOT NULL, return_id TEXT, status TEXT NOT NULL DEFAULT 'Pending', created_at TEXT NOT NULL);
"""
def db():
    c=sqlite3.connect(DB_PATH)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript(SCHEMA)

def norm(row):
    return {str(k).strip().lower(): (v.strip() if isinstance(v,str) else v) for k,v in row.items() if k is not None}

def read_csv(raw):
    text = raw.decode("utf-8-sig", errors="replace")
    return [norm(r) for r in csv.DictReader(io.StringIO(text))]

def num(v, default=0):
    try: return float(v) if v not in (None, "") else default
    except (ValueError, TypeError): return default

def import_zip(fileobj):
    allowed = {"products.csv","inventory.csv","vendor_terms.csv","returns.csv","sales.csv","refurbishment.csv","liquidation.csv"}
    try:
        archive = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError):
        return {"error":"That file is not a valid ZIP archive."}, 400
    found={}
    total=0
    for info in archive.infolist():
        if info.is_dir(): continue
        name=os.path.basename(info.filename).lower()
        if name not in allowed: continue
        total += info.file_size
        if total > 60*1024*1024: return {"error":"Uncompressed CSV files exceed 60 MB."}, 413
        found[name]=read_csv(archive.read(info))
    if "returns.csv" not in found:
        return {"error":"ZIP must contain returns.csv."}, 400
    required={
      "products.csv":{"sku","product","category","cost_price","selling_price","supplier"},
      "returns.csv":{"return_id","date_received","sku","supplier_batch","reason_code","condition_grade","status"},
      "inventory.csv":{"sku","new_stock","avg_daily_demand"},
      "vendor_terms.csv":{"supplier","sku","rtv_allowed","credit_pct","window_days"},
      "sales.csv":{"date","sku","qty_sold","selling_price"},
      "refurbishment.csv":{"category","refurb_cost","days_required","resale_pct_of_new"},
      "liquidation.csv":{"category","condition_grade","recovery_pct_of_cost"}}
    for name, cols in required.items():
        if name in found and found[name] and not cols.issubset(found[name][0].keys()):
            return {"error":f"{name} is missing columns: {', '.join(sorted(cols-found[name][0].keys()))}"}, 400
    # Import all provided files in a single transaction, replacing old imported dataset to avoid duplicates.
    with db() as c:
        for table in ("products","inventory","vendor_terms","returns","sales","refurbishment","liquidation"):
            c.execute(f"DELETE FROM {table}")
        for r in found.get("products.csv",[]):
            sku=str(r.get("sku","")).strip()
            if sku:
                c.execute("INSERT OR REPLACE INTO products VALUES(?,?,?,?,?,?)",(sku,r.get("product",""),r.get("category",""),num(r.get("cost_price")),num(r.get("selling_price")),r.get("supplier","")))
        for r in found.get("inventory.csv",[]):
            if r.get("sku"): c.execute("INSERT OR REPLACE INTO inventory VALUES(?,?,?)",(r["sku"],int(num(r.get("new_stock"))),num(r.get("avg_daily_demand"))))
        for r in found.get("vendor_terms.csv",[]):
            if r.get("sku") and r.get("supplier"):
                c.execute("INSERT OR REPLACE INTO vendor_terms VALUES(?,?,?,?,?)",(r["supplier"],r["sku"],r.get("rtv_allowed","No"),num(r.get("credit_pct")),int(num(r.get("window_days")))))
        for r in found.get("returns.csv",[]):
            if r.get("return_id") and r.get("sku"):
                c.execute("INSERT OR REPLACE INTO returns VALUES(?,?,?,?,?,?,?)",(r["return_id"],r.get("date_received",""),r["sku"],r.get("supplier_batch",""),r.get("reason_code",""),str(r.get("condition_grade","")).upper(),r.get("status","")))
        for r in found.get("sales.csv",[]):
            if r.get("sku"): c.execute("INSERT INTO sales(date,sku,qty_sold,selling_price) VALUES(?,?,?,?)",(r.get("date",""),r["sku"],num(r.get("qty_sold")),num(r.get("selling_price"))))
        for r in found.get("refurbishment.csv",[]):
            if r.get("category"): c.execute("INSERT OR REPLACE INTO refurbishment VALUES(?,?,?,?)",(r["category"],num(r.get("refurb_cost")),int(num(r.get("days_required"))),num(r.get("resale_pct_of_new"))))
        for r in found.get("liquidation.csv",[]):
            if r.get("category"): c.execute("INSERT OR REPLACE INTO liquidation VALUES(?,?,?)",(r["category"],str(r.get("condition_grade","")).upper(),num(r.get("recovery_pct_of_cost"))))
    counts={name:len(rows) for name,rows in found.items()}
    return {"message":"Dataset imported. Records are joined by SKU.", "files_processed":len(found),"imported":counts,"returns":counts.get("returns.csv",0)},200

def joined_returns(search="", grade="all"):
    query = """
      SELECT r.return_id,r.date_received,r.sku,r.supplier_batch,r.reason_code,r.condition_grade,r.status,
        COALESCE(NULLIF(p.product,''),'Unknown product') AS product,
        COALESCE(NULLIF(p.category,''),'Unknown category') AS category,
        COALESCE(NULLIF(p.supplier,''),NULLIF(v.supplier,''),'Unknown supplier') AS supplier,
        COALESCE(p.cost_price,0) AS cost_price, COALESCE(p.selling_price,0) AS selling_price,
        COALESCE(i.new_stock,0) AS new_stock, COALESCE(i.avg_daily_demand,0) AS avg_daily_demand,
        COALESCE(v.rtv_allowed,'No') AS rtv_allowed, COALESCE(v.credit_pct,0) AS credit_pct,
        COALESCE(v.window_days,0) AS window_days,
        COALESCE(f.refurb_cost,0) AS refurb_cost, COALESCE(f.days_required,0) AS refurb_days,
        COALESCE(f.resale_pct_of_new,0) AS resale_pct_of_new,
        COALESCE(l.recovery_pct_of_cost,0) AS liquidation_pct
      FROM returns r
      LEFT JOIN products p ON UPPER(TRIM(p.sku))=UPPER(TRIM(r.sku))
      LEFT JOIN inventory i ON UPPER(TRIM(i.sku))=UPPER(TRIM(r.sku))
      LEFT JOIN vendor_terms v ON UPPER(TRIM(v.sku))=UPPER(TRIM(r.sku)) AND v.supplier=COALESCE(NULLIF(p.supplier,''),v.supplier)
      LEFT JOIN refurbishment f ON f.category=p.category
      LEFT JOIN liquidation l ON l.category=p.category AND UPPER(l.condition_grade)=UPPER(r.condition_grade)
      WHERE (?='' OR lower(r.sku) LIKE ? OR lower(COALESCE(p.product,'')) LIKE ? OR lower(r.reason_code) LIKE ? OR lower(r.supplier_batch) LIKE ? OR lower(COALESCE(p.supplier,'')) LIKE ?)
        AND (?='all' OR upper(r.condition_grade)=upper(?))
      ORDER BY r.date_received DESC, r.return_id
    """
    term=search.strip().lower()
    with db() as c:
        rows=c.execute(query,(term, f"%{term}%",f"%{term}%",f"%{term}%",f"%{term}%",f"%{term}%",grade,grade)).fetchall()
    result=[]
    for rr in rows:
        r=dict(rr)
        gradev=r["condition_grade"]
        vendor_ok=str(r["rtv_allowed"]).lower() in ("yes","true","1","y")
        quantity=1
        options=[]
        if gradev in ("A","B","C") and r["selling_price"]>0:
            factor={"A":.95,"B":.82,"C":.48}.get(gradev,.5)
            options.append(("Restock",r["selling_price"]*factor,1,"Estimated resale based on condition grade."))
        if gradev in ("B","C") and r["selling_price"]>0:
            resale=r["selling_price"]*r["resale_pct_of_new"]-r["refurb_cost"]
            options.append(("Refurbish",max(0,resale),int(r["refurb_days"]),"Estimated resale after refurbishment cost."))
        if vendor_ok and r["cost_price"]>0:
            options.append(("Vendor return",r["cost_price"]*r["credit_pct"],2,"Estimated supplier credit from vendor terms."))
        if r["cost_price"]>0 and r["liquidation_pct"]>0:
            options.append(("Liquidate",r["cost_price"]*r["liquidation_pct"],2,"Estimated liquidation recovery from category and grade."))
        options.sort(key=lambda x:x[1],reverse=True)
        r["recommended_route"]=options[0][0] if options else "Inspect data"
        r["estimated_recovery"]=round(options[0][1],2) if options else 0
        r["days_to_cash"]=options[0][2] if options else None
        r["reasoning"]=options[0][3] if options else "No matching product economics or recovery rules were found. Check SKU and source files."
        r["match_status"]="Matched" if r["product"]!="Unknown product" else "SKU not found in products.csv"
        r["vendor_eligible"]=vendor_ok
        result.append(r)
    return result

@app.get("/")
def home():
    return send_from_directory(BASE,"index.html")

@app.get("/api/health")
def health():
    with db() as c:
        n=c.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
    return jsonify({"status":"ok","returns_loaded":n})

@app.post("/api/import-zip")
def import_zip_route():
    uploaded=request.files.get("file")
    if not uploaded: return jsonify({"error":"Choose the ZIP dataset first."}),400
    result,status=import_zip(uploaded.stream)
    return jsonify(result),status

@app.get("/api/returns")
def api_returns():
    data=joined_returns(request.args.get("search",""),request.args.get("grade","all"))
    return jsonify(data)

@app.get("/api/dashboard")
def api_dashboard():
    with db() as c:
        counts=c.execute("SELECT COUNT(*) returns_count,COUNT(DISTINCT sku) sku_count FROM returns").fetchone()
        unknown=c.execute("SELECT COUNT(*) FROM returns r LEFT JOIN products p ON UPPER(TRIM(p.sku))=UPPER(TRIM(r.sku)) WHERE p.sku IS NULL").fetchone()[0]
        supplier_count=c.execute("SELECT COUNT(DISTINCT supplier) FROM products").fetchone()[0]
    rows=joined_returns()
    return jsonify({"return_records":counts["returns_count"],"unique_skus":counts["sku_count"],"unknown_product_records":unknown,"supplier_count":supplier_count,"estimated_recovery":round(sum(x["estimated_recovery"] for x in rows),2),"matched_records":counts["returns_count"]-unknown})

@app.get("/api/suppliers")
def api_suppliers():
    with db() as c:
        rows=c.execute("""SELECT COALESCE(p.supplier,'Unknown supplier') supplier, COUNT(*) returns_count,
          SUM(CASE WHEN p.sku IS NULL THEN 1 ELSE 0 END) unmatched
          FROM returns r LEFT JOIN products p ON UPPER(TRIM(p.sku))=UPPER(TRIM(r.sku))
          GROUP BY COALESCE(p.supplier,'Unknown supplier') ORDER BY returns_count DESC""").fetchall()
    return jsonify([dict(x) for x in rows])


@app.get("/api/attention")
def api_attention():
    # Daily attention is derived from imported records; no synthetic demo facts.
    with db() as c:
        rows=c.execute("""SELECT r.supplier_batch, r.reason_code, COUNT(*) AS records,
          COUNT(DISTINCT r.sku) AS distinct_skus,
          COALESCE(NULLIF(p.supplier,''),'Unknown supplier') AS supplier,
          GROUP_CONCAT(DISTINCT r.sku) AS skus
          FROM returns r LEFT JOIN products p ON UPPER(TRIM(p.sku))=UPPER(TRIM(r.sku))
          GROUP BY r.supplier_batch,r.reason_code,COALESCE(NULLIF(p.supplier,''),'Unknown supplier')
          HAVING COUNT(*) >= 3 ORDER BY records DESC LIMIT 100""").fetchall()
    result=[]
    for x in rows:
        d=dict(x); d['priority']='High' if d['records']>=8 else ('Review' if d['records']>=5 else 'Monitor')
        d['recommendation']='Inspect affected units and review supplier terms before approving recovery actions.'
        result.append(d)
    return jsonify(result)

@app.post("/api/returns")
def api_add_return():
    data=request.get_json(silent=True) or {}
    sku=str(data.get('sku','')).strip()
    reason=str(data.get('reason_code','')).strip()
    grade=str(data.get('condition_grade','')).strip().upper()
    batch=str(data.get('supplier_batch','')).strip()
    date_received=str(data.get('date_received','')).strip() or datetime.now().date().isoformat()
    status=str(data.get('status','Pending')).strip() or 'Pending'
    if not sku or not reason or not batch or grade not in ('A','B','C','D'):
        return jsonify({'error':'SKU, batch, reason and a condition grade (A/B/C/D) are required.'}),400
    with db() as c:
        # Preserve the supplied SKU and source product names; never invent a match.
        base='RTN-MANUAL'
        n=1
        rid=f'{base}-{datetime.now().strftime("%Y%m%d%H%M%S")}'
        while c.execute('SELECT 1 FROM returns WHERE return_id=?',(rid,)).fetchone():
            n+=1; rid=f'{base}-{datetime.now().strftime("%Y%m%d%H%M%S")}-{n}'
        c.execute('INSERT INTO returns VALUES(?,?,?,?,?,?,?)',(rid,date_received,sku,batch,reason,grade,status))
    row=joined_returns(search=sku,grade='all')
    created=next((x for x in row if x['return_id']==rid),None)
    return jsonify({'message':'Return added successfully.','return':created or {'return_id':rid,'sku':sku,'reason_code':reason,'condition_grade':grade}}),201

@app.get("/api/approvals")
def api_get_approvals():
    with db() as c:
        rows=c.execute('SELECT * FROM approvals ORDER BY id DESC').fetchall()
    return jsonify([dict(x) for x in rows])

@app.post("/api/approvals")
def api_create_approval():
    data=request.get_json(silent=True) or {}
    title=str(data.get('title','')).strip(); detail=str(data.get('detail','')).strip(); return_id=str(data.get('return_id','')).strip() or None
    if not title or not detail: return jsonify({'error':'Title and details are required.'}),400
    with db() as c:
        cur=c.execute('INSERT INTO approvals(title,detail,return_id,status,created_at) VALUES(?,?,?,\'Pending\',?)',(title,detail,return_id,datetime.now().isoformat(timespec='seconds')))
        aid=cur.lastrowid
        row=c.execute('SELECT * FROM approvals WHERE id=?',(aid,)).fetchone()
    return jsonify(dict(row)),201

@app.patch("/api/approvals/<int:approval_id>")
def api_update_approval(approval_id):
    data=request.get_json(silent=True) or {}; status=str(data.get('status',''))
    if status not in ('Approved','Rejected'): return jsonify({'error':'Status must be Approved or Rejected.'}),400
    with db() as c:
        cur=c.execute('UPDATE approvals SET status=? WHERE id=? AND status=\'Pending\'',(status,approval_id))
        if cur.rowcount==0: return jsonify({'error':'Pending approval not found.'}),404
        row=c.execute('SELECT * FROM approvals WHERE id=?',(approval_id,)).fetchone()
    return jsonify(dict(row))


init_db()
if __name__=="__main__":
    app.run(host="127.0.0.1",port=5000,debug=True)
