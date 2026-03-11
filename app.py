from flask import Flask, render_template, request, redirect, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_socketio import SocketIO
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import io
import datetime
import os
from openpyxl import Workbook
from sqlalchemy import or_
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey")

app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_HTTPONLY"] = True

UPLOAD_FOLDER = "uploads"
TEMPLATE_FOLDER = "templates_docs"

app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///documents.db")
db = SQLAlchemy(app)

socketio = SocketIO(app, async_mode="threading")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = None


# ---------------- USER MODEL ----------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    uid = db.Column(db.String(50))


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------- DOCUMENT MODEL ----------------
class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200))
    reference = db.Column(db.String(100))
    status = db.Column(db.String(100))
    doc_type = db.Column(db.String(50))
    signer1 = db.Column(db.String(100))
    signer2 = db.Column(db.String(100))
    pdf_data = db.Column(db.LargeBinary)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


# ---------------- LOGIN ----------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form.get('name')
        uid = request.form.get('uid')
        remember = True if request.form.get('remember') else False

        user = User.query.filter_by(uid=uid).first()
        if not user:
            user = User(name=name, uid=uid)
            db.session.add(user)
            db.session.commit()

        login_user(user, remember=remember)
        return redirect('/home')

    return render_template("login.html")


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')


# ---------------- HOME ----------------
@app.route('/')
def root():
    return redirect('/login')


@app.route('/home', methods=['GET', 'POST'])
@login_required
def home():

    if request.method == 'POST':

        action = request.form.get('action')
        reference = request.form.get('reference', '').strip()

        if not reference:
            return "Reference required"

        existing = Document.query.filter(
            Document.reference == reference,
            Document.status.contains("Awaiting")
        ).first()

        if existing:
            return f"Duplicate reference {reference} already awaiting signature."

        if action in ["qo1", "po1"]:

            template_file = "QO_Template.pdf" if action == "qo1" else "PO_Template.pdf"
            prefix = "QO" if action == "qo1" else "PO"

            template_path = os.path.join(TEMPLATE_FOLDER, template_file)
            if not os.path.exists(template_path):
                return f"Missing {template_file}"

            if action == "qo1":
                x, y = 90, 505
            elif action == "po1":
                x, y = 110, 270

            filename, pdf_data = sign_pdf(
                template_path,
                current_user.name,
                current_user.uid,
                reference,
                x,
                y,
                prefix,
                "1st Sign"
            )

            doc = Document(
                filename=filename,
                reference=reference,
                status=f"Awaiting {prefix} 2nd Sign",
                doc_type=prefix,
                signer1=current_user.name,
                pdf_data=pdf_data
            )

            db.session.add(doc)
            db.session.commit()

            return send_file(
                io.BytesIO(pdf_data),
                download_name=filename,
                as_attachment=True,
                mimetype="application/pdf"
            )

        elif action == "anfo1":

            pdf_file = request.files.get('pdf')
            if not pdf_file:
                return "Upload document"

            filepath = os.path.join(UPLOAD_FOLDER, pdf_file.filename)
            pdf_file.save(filepath)

            signed_path = sign_pdf(filepath, current_user.name, current_user.uid,
                                   reference, 90, 230, "ANFO", "1st Sign")

            doc = Document(
                filename=os.path.basename(signed_path),
                reference=reference,
                status="Awaiting ANFO 2nd Sign",
                doc_type="ANFO",
                signer1=current_user.name
            )

            db.session.add(doc)
            db.session.commit()

            return send_file(signed_path, as_attachment=True)

    return render_template("index.html", user=current_user)


# ---------------- DASHBOARD ----------------
@app.route('/dashboard')
@login_required
def dashboard():

    filter_type = request.args.get("filter", "open")
    search_query = request.args.get("search", "").strip()
    now = datetime.datetime.now()
    two_min_ago = now - datetime.timedelta(minutes=2)

    query = Document.query

    if search_query:
        query = query.filter(
            or_(
                Document.reference.contains(search_query),
                Document.signer1.contains(search_query),
                Document.signer2.contains(search_query)
            )
        )

    if filter_type == "open":
        query = query.filter(
            (Document.status.contains("Awaiting")) |
            (
                (Document.completed_at != None) &
                (Document.completed_at >= two_min_ago)
            )
        )

    elif filter_type == "completed":
        query = query.filter(Document.completed_at != None)

    elif filter_type == "qo":
        query = query.filter(Document.doc_type == "QO")

    elif filter_type == "po":
        query = query.filter(Document.doc_type == "PO")

    elif filter_type == "anfo":
        query = query.filter(Document.doc_type == "ANFO")

    documents = query.order_by(Document.created_at.desc()).all()

    pending_count = Document.query.filter(
        Document.status.contains("Awaiting")
    ).count()

    return render_template(
        "dashboard.html",
        documents=documents,
        pending_count=pending_count,
        filter_type=filter_type,
        search_query=search_query
    )


# ---------------- DELETE DOCUMENT ----------------
@app.route('/delete/<int:doc_id>')
@login_required
def delete_document(doc_id):

    document = Document.query.get(doc_id)

    if document:

        filepath = os.path.join(UPLOAD_FOLDER, document.filename)
        if os.path.exists(filepath):
            os.remove(filepath)

        db.session.delete(document)
        db.session.commit()

    return redirect('/dashboard')


# ---------------- SIGN SECOND ----------------
@app.route('/sign/<int:doc_id>')
@login_required
def sign_second(doc_id):

    document = Document.query.get(doc_id)

    if document and "Awaiting" in document.status:

        pdf_stream = io.BytesIO(document.pdf_data)

        if document.doc_type == "QO":
            x, y = 399, 505
        elif document.doc_type == "PO":
            x, y = 410, 270
        else:
            x, y = 399, 230

        filename, pdf_data = sign_pdf(
            pdf_stream,
            current_user.name,
            current_user.uid,
            document.reference,
            x,
            y,
            document.doc_type,
            "2nd Sign"
        )

        document.status = "Completed"
        document.signer2 = current_user.name
        document.filename = filename
        document.pdf_data = pdf_data
        document.completed_at = datetime.datetime.now()

        db.session.commit()

        return send_file(
            io.BytesIO(pdf_data),
            download_name=filename,
            as_attachment=True,
            mimetype="application/pdf"
        )

    return redirect('/dashboard')


# ---------------- SIGN FUNCTION ----------------
def sign_pdf(filepath, name, uid, reference, x, y, prefix, stage):

    timestamp = datetime.datetime.now().strftime("%d-%b-%Y %H:%M")

    packet = io.BytesIO()
    can = canvas.Canvas(packet)
    can.setFont("Helvetica", 8)

    can.drawString(x, y, f"Signed by: {name}")
    can.drawString(x, y - 12, f"UID: {uid}")
    can.drawString(x, y - 24, f"Date: {timestamp}")
    can.drawString(x, y - 36, f"Ref: {reference}")

    can.save()
    packet.seek(0)

    overlay_pdf = PdfReader(packet)

    if isinstance(filepath, io.BytesIO):
        original_pdf = PdfReader(filepath)
    else:
        original_pdf = PdfReader(filepath)

    writer = PdfWriter()
    page = original_pdf.pages[0]
    page.merge_page(overlay_pdf.pages[0])
    writer.add_page(page)

    filename = f"{prefix} {reference} - {stage}.pdf"

    pdf_bytes = io.BytesIO()
    writer.write(pdf_bytes)
    pdf_bytes.seek(0)

    return filename, pdf_bytes.read()


# Ensure database tables exist
with app.app_context():
    db.drop_all()
    db.create_all()


if __name__ == '__main__':
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)
    if not os.path.exists(TEMPLATE_FOLDER):
        os.makedirs(TEMPLATE_FOLDER)
    with app.app_context():
        db.create_all()
    socketio.run(app)