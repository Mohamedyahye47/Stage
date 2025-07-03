from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, jsonify
import os
import PyPDF2
import re
from datetime import datetime
import mysql.connector
from flask_bcrypt import Bcrypt
from werkzeug.utils import secure_filename
import openpyxl
from io import BytesIO
import pandas as pd
import numpy as np
from scipy.stats import chi2_contingency
import logging
from decimal import Decimal
from jinja2 import Undefined

# Configure logging
logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

app = Flask(__name__)
bcrypt = Bcrypt(app)
app.secret_key = os.environ.get('FLASK_SECRET', os.urandom(24))
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['PROFILE_UPLOAD_FOLDER'] = 'static/uploads/profiles'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}
app.config['ALLOWED_PROFILE_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

db_config = {
    'host': os.environ.get('MYSQL_HOST', 'localhost'),
    'user': os.environ.get('MYSQL_USER', 'root'),
    'password': os.environ.get('MYSQL_PASSWORD', 'MedYahya47!!'),
    'database': os.environ.get('MYSQL_DB', 'data_upload')
}

def format_number(value):
    try:
        return "{:,.2f}".format(float(value)).replace(",", " ").replace(".", ",").replace("'", " ")
    except (ValueError, TypeError):
        return "0,00"

app.jinja_env.filters['format_number'] = format_number

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def allowed_profile_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_PROFILE_EXTENSIONS']

def get_connection():
    try:
        conn = mysql.connector.connect(**db_config)
        logger.info("Successfully connected to database")
        return conn
    except mysql.connector.Error as err:
        logger.error(f"Database connection error: {str(err)}")
        flash(f'Erreur de connexion à la base de données : {str(err)}', 'danger')
        raise

def convert_decimals(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, Undefined):
        logger.warning(f"Encountered Undefined object: {obj}")
        return None
    elif isinstance(obj, str):
        try:
            return float(obj) if '.' in obj or 'e' in obj.lower() else int(obj)
        except ValueError:
            return obj
    elif isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [convert_decimals(item) for item in obj]
    elif obj is None:
        return None
    else:
        logger.warning(f"Unhandled type in convert_decimals: {type(obj)}")
        return obj

def extract_invoice_data(file_stream):

    text = ""
    pdf_reader = PyPDF2.PdfReader(file_stream)
    for page in pdf_reader.pages:
        text += page.extract_text() + "\n"

    lines = text.splitlines()
    societe = None

    # Extract company name
    for i, line in enumerate(lines):
        if "Contrepartie" in line:
            if ':' in line:
                parts = line.split(':', 1)
                if len(parts) > 1 and parts[1].strip():
                    societe = parts[1].strip()
                elif i + 1 < len(lines):
                    societe = lines[i + 1].strip()
            elif i + 1 < len(lines):
                societe = lines[i + 1].strip()
            break

    # Extract product
    product_match = re.search(r'PRODUIT\s*\|\s*([^\|]+)', text) or \
                   re.search(r'Produit:\s*([^\n]+)', text)
    produit = product_match.group(1).strip() if product_match else None

    # Extract quantity
    quantite_match = re.search(
        r'(?:QUANTITE\s*\|\s*|Quantité \(Tonnes Métriques\)\s*|Quantity:\s*)([\d\',\.]+)\s*(?:MT|Tonnes|TM)?',
        text, re.IGNORECASE
    )
    quantite = float(quantite_match.group(1).replace("'", "").replace(",", "").replace(" ", "")) if quantite_match else 0

    # Extract totals
    total_usd_match = re.search(r'Montant total de la facture\s*\$([\d\',]+\.\d{2})', text)
    total_usd = float(total_usd_match.group(1).replace("'", "").replace(",", "")) if total_usd_match else 0

    fret_match = re.search(r'FRET USD / Tonne Métrique\s*\$([\d\.,]+)', text)
    fret = float(fret_match.group(1).replace(",", "")) if fret_match else 0

    total_sans_fret = round(total_usd - (fret * quantite), 2) if total_usd and fret and quantite else 0

    # Extract date
    invoice_date = None
    try:
        date_match = re.search(r'Date du Bordereau de cession en bac:\s*(\d{2}\.\d{2}\.\d{4})', text) or \
                    re.search(r'Date du Bordereau de cession en bac:\s*(\d{2}/\d{2}/\d{4})', text)
        if date_match:
            date_str = date_match.group(1).replace('.', '/')  # Normalize date separators
            invoice_date = datetime.strptime(date_str, '%d/%m/%Y').date()
    except (ValueError, AttributeError) as e:
        logger.error(f"Error parsing date: {e}")

    return {
        'invoice_date': invoice_date,
        'destination': re.search(r'Terminal:\s*([^\n]+)', text).group(1).split()[0] if re.search(
            r'Terminal:\s*([^\n]+)', text) else None,
        'societe': societe,
        'produit': produit,
        'quantite': quantite,
        'prix_unitaire': float(
            re.search(r'Prix Unitaire\s+\$([\d\',]+\.\d{2})', text).group(1).replace("'", "").replace(",", "")
        ) if re.search(r'Prix Unitaire\s+\$([\d\',]+\.\d{2})', text) else 0,
        'total_usd': total_usd,
        'fret': fret,
        'total_sans_fret': total_sans_fret
    }


def get_dashboard_data(offset=0, limit=10, search_query=None, selected_month=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Fetch available months
        cursor.execute("""
            SELECT DISTINCT DATE_FORMAT(invoice_date, '%Y-%m') as month
            FROM invoices
            WHERE invoice_date IS NOT NULL
            ORDER BY month DESC
        """)
        available_months = [row['month'] for row in cursor.fetchall()]

        # Table where clause (includes search_query and selected_month)
        where_clause = " WHERE 1=1"
        table_params = []  # Paramètres pour les requêtes de table

        if selected_month:
            where_clause += " AND DATE_FORMAT(i.invoice_date, '%Y-%m') = %s"
            table_params.append(selected_month)

        if search_query and search_query.strip():
            search_query_like = f"%{search_query}%"
            where_clause += """
                AND (
                    i.ot_number LIKE %s OR
                    DATE_FORMAT(i.invoice_date, '%%Y-%%m-%%d') LIKE %s OR
                    s.nom LIKE %s OR
                    p.nom LIKE %s OR
                    d.nom LIKE %s
                )
            """
            table_params.extend([search_query_like] * 5)

        # Total count (pour pagination) - utilise les mêmes paramètres que la table
        cursor.execute(f"""
            SELECT COUNT(*) as total_count
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            JOIN destination d ON i.destination_id = d.id
            {where_clause}
        """, table_params)
        total_count = cursor.fetchone()['total_count']

        # Main table query (avec search et pagination)
        query = f"""
            SELECT 
                i.ot_number,
                DATE_FORMAT(i.invoice_date, '%Y-%m-%d') as invoice_date,
                s.nom AS societe,
                p.nom AS produit,
                d.nom AS destination,
                COALESCE(i.quantite, 0) AS quantite,
                COALESCE(i.total_usd, 0) AS total_usd,
                i.total_sans_fret
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            JOIN destination d ON i.destination_id = d.id
            {where_clause}
            ORDER BY i.created_at DESC
            LIMIT %s OFFSET %s
        """
        # Ajouter limit et offset aux paramètres de table
        main_query_params = table_params + [limit, offset]
        logger.debug(f"Executing table query: {query} with params {main_query_params}")
        cursor.execute(query, main_query_params)
        invoices = cursor.fetchall()

        # Graph/stats where clause (seulement selected_month pour les stats)
        graph_clause = " WHERE 1=1"
        graph_params = []
        if selected_month:
            graph_clause += " AND DATE_FORMAT(i.invoice_date, '%Y-%m') = %s"
            graph_params.append(selected_month)

        # Stats query (total invoices, total value, avg value)
        cursor.execute(f"""
            SELECT 
                COUNT(*) as total_invoices,
                COALESCE(SUM(i.total_usd), 0) as total_value,
                COALESCE(AVG(i.total_usd), 0) as avg_value
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            JOIN destination d ON i.destination_id = d.id
            {graph_clause}
        """, graph_params)
        stats = cursor.fetchone() or {
            'total_invoices': 0,
            'total_value': 0,
            'avg_value': 0
        }

        # Top societe query
        cursor.execute(f"""
            SELECT 
                s.nom as societe,
                COALESCE(SUM(i.total_usd), 0) as total_usd,
                (
                    COALESCE(SUM(i.total_usd), 0) /
                    NULLIF((
                        SELECT SUM(sub_i.total_usd)
                        FROM invoices sub_i
                        JOIN societe sub_s ON sub_i.societe_id = sub_s.id
                        JOIN produit sub_p ON sub_i.produit_id = sub_p.id
                        JOIN destination sub_d ON sub_i.destination_id = sub_d.id
                        {graph_clause.replace('i.', 'sub_i.').replace('s.', 'sub_s.').replace('p.', 'sub_p.').replace('d.', 'sub_d.')}
                    ), 0) * 100
                ) as percentage
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            JOIN destination d ON i.destination_id = d.id
            {graph_clause}
            GROUP BY i.societe_id, s.nom
            ORDER BY total_usd DESC
            LIMIT 1
        """, graph_params * 2)  # graph_params utilisé deux fois dans la requête
        top_societe = cursor.fetchone()
        stats['top_societe_name'] = top_societe['societe'] if top_societe else 'N/A'
        stats['top_societe_percent'] = round(top_societe['percentage'], 1) if top_societe else 0

        # Monthly data
        cursor.execute(f"""
            SELECT 
                DATE_FORMAT(i.invoice_date, '%Y-%m') as month,
                COALESCE(SUM(i.total_usd), 0) as total
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            JOIN destination d ON i.destination_id = d.id
            {graph_clause}
            GROUP BY month
            ORDER BY month
        """, graph_params)
        monthly_data = cursor.fetchall()

        # Monthly societe data for Cramér's V
        cursor.execute(f"""
            SELECT 
                DATE_FORMAT(i.invoice_date, '%Y-%m') as month,
                s.nom as societe,
                COALESCE(SUM(i.total_usd), 0) as total
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            {graph_clause}
            GROUP BY month, i.societe_id
        """, graph_params)
        monthly_societe_data = cursor.fetchall()
        cramers_v_monthly = None
        if monthly_societe_data and len(monthly_societe_data) > 1:
            try:
                monthly_df = pd.DataFrame(monthly_societe_data)
                contingency_table = pd.crosstab(monthly_df['month'], monthly_df['societe'])
                if contingency_table.shape[0] > 1 and contingency_table.shape[1] > 1:
                    chi2, _, _, _ = chi2_contingency(contingency_table)
                    n = contingency_table.sum().sum()
                    phi2 = chi2 / n
                    r, k = contingency_table.shape
                    cramers_v_monthly = np.sqrt(phi2 / min((k - 1), (r - 1)))
            except ValueError:
                cramers_v_monthly = None

        # Product data
        cursor.execute(f"""
            SELECT 
                p.nom as produit,
                COALESCE(SUM(i.quantite), 0) as total_quantite,
                COALESCE(SUM(i.total_usd), 0) as total_usd
            FROM invoices i
            JOIN produit p ON i.produit_id = p.id
            {graph_clause}
            GROUP BY i.produit_id
        """, graph_params)
        product_data_raw = cursor.fetchall()
        total_quantite = sum(row['total_quantite'] for row in product_data_raw) or 1
        product_data = [{
            'produit': row['produit'],
            'total_quantite': row['total_quantite'],
            'total_usd': row['total_usd'],
            'percentage': round((row['total_quantite'] / total_quantite) * 100, 1)
        } for row in product_data_raw]

        # Societe data
        cursor.execute(f"""
            SELECT 
                s.nom as societe,
                COALESCE(SUM(i.quantite), 0) as total_quantite,
                COALESCE(SUM(i.total_usd), 0) as total_usd
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            {graph_clause}
            GROUP BY i.societe_id
        """, graph_params)
        societe_data_raw = cursor.fetchall()
        total_quantite_all = sum(row['total_quantite'] for row in societe_data_raw) or 1
        societe_labels = [row['societe'] for row in societe_data_raw]
        societe_pourcentages = [
            round((row['total_quantite'] / total_quantite_all) * 100, 1) for row in societe_data_raw
        ]

        # Societe vs Destination data
        cursor.execute(f"""
            SELECT 
                s.nom as societe,
                d.nom as destination,
                COALESCE(SUM(i.total_usd), 0) as total_usd
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN destination d ON i.destination_id = d.id
            {graph_clause}
            GROUP BY i.societe_id, i.destination_id
        """, graph_params)
        destination_data = cursor.fetchall()
        societe_destination_datasets = []
        cramers_v_societe_destination = None
        if destination_data:
            societe_totals = {}
            for row in destination_data:
                societe = row['societe']
                societe_totals[societe] = societe_totals.get(societe, 0) + row['total_usd']
            destinations = sorted(set(row['destination'] for row in destination_data))
            colors = ['#4e73df', '#1cc88a', '#36b9cc', '#f6c23e', '#e74a3b']
            for i, dest in enumerate(destinations):
                dest_data = [row for row in destination_data if row['destination'] == dest]
                percentages = []
                for societe in societe_labels:
                    matching = next((row for row in dest_data if row['societe'] == societe), None)
                    total = societe_totals.get(societe, 1)
                    percentage = (matching['total_usd'] / total * 100) if matching else 0
                    percentages.append(round(percentage, 1))
                societe_destination_datasets.append({
                    'label': dest,
                    'data': percentages,
                    'backgroundColor': colors[i % len(colors)],
                    'borderColor': colors[i % len(colors)],
                    'borderWidth': 1
                })
            if len(destination_data) >= 2:
                try:
                    df = pd.DataFrame(destination_data)
                    contingency_table = pd.crosstab(df['societe'], df['destination'])
                    if contingency_table.shape[0] > 1 and contingency_table.shape[1] > 1:
                        chi2, _, _, _ = chi2_contingency(contingency_table)
                        n = contingency_table.sum().sum()
                        phi2 = chi2 / n
                        r, k = contingency_table.shape
                        cramers_v_societe_destination = np.sqrt(phi2 / min((k - 1), (r - 1)))
                except ValueError:
                    cramers_v_societe_destination = None

        # Produit vs Destination data
        cursor.execute(f"""
            SELECT 
                p.nom as produit,
                d.nom as destination,
                COALESCE(SUM(i.total_usd), 0) as total_usd
            FROM invoices i
            JOIN produit p ON i.produit_id = p.id
            JOIN destination d ON i.destination_id = d.id
            {graph_clause}
            GROUP BY i.produit_id, i.destination_id
            HAVING SUM(i.total_usd) > 0
        """, graph_params)
        produit_destination_data = cursor.fetchall()
        produit_destination_datasets = []
        cramers_v_produit_destination = None
        produits = sorted(set(row['produit'] for row in produit_destination_data))
        if produit_destination_data:
            produit_totals = {}
            for row in produit_destination_data:
                produit = row['produit']
                produit_totals[produit] = produit_totals.get(produit, 0) + row['total_usd']
            destinations = sorted(set(row['destination'] for row in produit_destination_data))
            colors = ['#4C78A8', '#F58518', '#E45756', '#72B7B2', '#6B4E31']
            for i, dest in enumerate(destinations):
                dest_data = [row for row in produit_destination_data if row['destination'] == dest]
                percentages = []
                for produit in produits:
                    matching = next((row for row in dest_data if row['produit'] == produit), None)
                    total = produit_totals.get(produit, 1)
                    percentage = (matching['total_usd'] / total * 100) if matching else 0
                    percentages.append(round(percentage, 1))
                produit_destination_datasets.append({
                    'label': dest,
                    'data': percentages,
                    'backgroundColor': colors[i % len(colors)],
                    'borderColor': colors[i % len(colors)],
                    'borderWidth': 1
                })
            if len(produit_destination_data) >= 2:
                try:
                    df = pd.DataFrame(produit_destination_data)
                    contingency_table = pd.crosstab(df['produit'], df['destination'])
                    if contingency_table.shape[0] > 1 and contingency_table.shape[1] > 1:
                        chi2, _, _, _ = chi2_contingency(contingency_table)
                        n = contingency_table.sum().sum()
                        phi2 = chi2 / n
                        r, k = contingency_table.shape
                        cramers_v_produit_destination = np.sqrt(phi2 / min((k - 1), (r - 1)))
                except ValueError:
                    cramers_v_produit_destination = None

        # Produit vs Societe data
        cursor.execute(f"""
            SELECT 
                s.nom as societe,
                p.nom as produit,
                COALESCE(SUM(i.total_usd), 0) as total_usd
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            {graph_clause}
            GROUP BY i.societe_id, i.produit_id
        """, graph_params)
        product_societe_data = cursor.fetchall()
        produit_societe_datasets = []
        cramers_v = None
        if product_societe_data and len(product_societe_data) >= 2:
            df_ps = pd.DataFrame(product_societe_data)
            produits_ps = sorted(df_ps['produit'].unique())
            try:
                if len(df_ps['societe'].unique()) > 1 and len(produits_ps) > 1:
                    contingency_table = pd.crosstab(df_ps['societe'], df_ps['produit'])
                    chi2, _, _, _ = chi2_contingency(contingency_table)
                    n = contingency_table.sum().sum()
                    phi2 = chi2 / n
                    r, k = contingency_table.shape
                    cramers_v = np.sqrt(phi2 / min((k - 1), (r - 1)))
            except ValueError:
                cramers_v = None
            colors = ['#4C78A8', '#F58518', '#E45756', '#72B7B2']
            societe_totals = df_ps.groupby('societe')['total_usd'].sum().to_dict()
            for i, produit in enumerate(produits_ps):
                produit_data = df_ps[df_ps['produit'] == produit]
                percentages = []
                for societe in societe_labels:
                    usd = produit_data[produit_data['societe'] == societe]['total_usd'].sum()
                    total = societe_totals.get(societe, 1)
                    percentage = (usd / total * 100) if total > 0 else 0
                    percentages.append(round(percentage, 1))
                produit_societe_datasets.append({
                    'label': produit,
                    'data': percentages,
                    'backgroundColor': colors[i % len(colors)],
                    'borderColor': colors[i % len(colors)],
                    'borderWidth': 1
                })

        return {
            'invoices': invoices or [],
            'stats': stats or {
                'total_invoices': 0,
                'total_value': 0,
                'avg_value': 0,
                'top_societe_name': 'N/A',
                'top_societe_percent': 0
            },
            'monthly_data': monthly_data or [],
            'product_data': product_data or [],
            'societe_labels': societe_labels or [],
            'societe_pourcentages': societe_pourcentages or [],
            'societe_destination_datasets': societe_destination_datasets or [],
            'produit_societe_datasets': produit_societe_datasets or [],
            'produit_destination_datasets': produit_destination_datasets or [],
            'produits': produits or [],
            'cramers_v': cramers_v,
            'cramers_v_monthly': cramers_v_monthly,
            'cramers_v_societe_destination': cramers_v_societe_destination,
            'cramers_v_produit_destination': cramers_v_produit_destination,
            'available_months': available_months or [],
            'selected_month': selected_month,
            'search_query': search_query or '',
            'total_count': total_count,
            'offset': offset
        }

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def get_invoices_table(offset=0, limit=10, search_query=None, selected_month=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        where_clause = " WHERE 1=1"
        params = []
        if selected_month:
            where_clause += " AND DATE_FORMAT(i.invoice_date, '%Y-%m') = %s"
            params.append(selected_month)
        if search_query and search_query.strip():
            search_query = f"%{search_query}%"
            where_clause += " AND (i.ot_number LIKE %s OR s.nom LIKE %s OR p.nom LIKE %s OR DATE_FORMAT(i.invoice_date, '%%Y-%%m-%%d') LIKE %s)"
            params.extend([search_query] * 4)

        query = f"""
            SELECT 
                i.ot_number,
                DATE_FORMAT(i.invoice_date, '%Y-%m-%d') as invoice_date,
                s.nom AS societe,
                p.nom AS produit,
                COALESCE(i.quantite, 0) AS quantite,
                COALESCE(i.total_usd, 0) AS total_usd,
                i.total_sans_fret
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            {where_clause}
            ORDER BY i.created_at DESC 
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        logger.debug(f"Executing table query: {query} with params {params}")
        cursor.execute(query, params)
        invoices = cursor.fetchall()

        cursor.execute(f"""
            SELECT COUNT(*) as total_count
            FROM invoices i
            JOIN societe s ON i.societe_id = s.id
            JOIN produit p ON i.produit_id = p.id
            {where_clause}
        """, params[:-2])
        total_count = cursor.fetchone()['total_count']

        return render_template('invoices_table.html', invoices=invoices, offset=offset, total_count=total_count,
                               search_query=search_query, selected_month=selected_month)
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/login', methods=["GET", "POST"])
def login():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form["email"]
        password = request.form['password']

        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, username, password, photo_profil, is_admin
                FROM users WHERE email = %s
            """, (email,))
            user = cursor.fetchone()

            if user and bcrypt.check_password_hash(user['password'], password):
                session['user_id'] = user['id']
                session['username'] = user['username']
                session['photo_profil'] = user['photo_profil']
                session['is_admin'] = user['is_admin']
                logger.info(f"User ID {user['id']} logged in successfully")
                flash('Connexion réussie !', 'success')
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            else:
                logger.warning(f"Failed login attempt for email {email}")
                flash('Email ou mot de passe incorrect', 'danger')
        except mysql.connector.Error as err:
            logger.error(f"Database error in login: {str(err)}")
            flash(f'Erreur de base de données : {str(err)}', 'danger')
        finally:
            if 'cursor' in locals():
                cursor.close()
            if 'conn' in locals():
                conn.close()

    return render_template('login.html')


@app.route('/register', methods=["GET", "POST"])
def register():
    if not session.get('is_admin'):
        flash('Accès refusé. Seuls les admins peuvent créer des comptes.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = bcrypt.generate_password_hash(request.form['password']).decode('utf-8')
        is_admin = 1 if request.form.get('role') == 'admin' else 0

        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                logger.warning(f"Registration failed: Email {email} already exists")
                flash('Cet email est déjà utilisé', 'danger')
                return redirect(url_for('register'))

            cursor.execute(
                "INSERT INTO users (username, email, password, is_admin) VALUES (%s, %s, %s, %s)",
                (username, email, password, is_admin)
            )
            conn.commit()

            logger.info(f"New {'admin' if is_admin else 'user'} registered: {username} (email: {email})")
            flash(f'Compte {"admin" if is_admin else "utilisateur"} créé avec succès !', 'success')
            return redirect(url_for('dashboard'))

        except mysql.connector.Error as err:
            logger.error(f"Database error in register: {str(err)}")
            flash(f'Erreur de base de données : {str(err)}', 'danger')
            return redirect(url_for('register'))
        finally:
            if 'cursor' in locals():
                cursor.close()
            if 'conn' in locals():
                conn.close()

    return render_template('register.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour accéder au tableau de bord.', 'danger')
        return redirect(url_for('login'))

    try:
        search_query = request.args.get('q', '')
        selected_month = request.args.get('month', '')
        offset = request.args.get('offset', type=int, default=0)
        data = get_dashboard_data(offset=offset, search_query=search_query, selected_month=selected_month)
        table_html = get_invoices_table(offset=offset, search_query=search_query, selected_month=selected_month)
        user_id = session.get('user_id', 'Unknown')
        logger.info(f"User ID {user_id} accessed dashboard")
        return render_template('dashboard.html', **data, table_html=table_html)
    except Exception as e:
        logger.error(f"Error in dashboard route: {str(e)}")
        flash(f'Erreur dans la route du tableau de bord : {str(e)}', 'danger')
        return render_template('dashboard.html',
                               invoices=[],
                               stats={
                                   'total_invoices': 0,
                                   'total_value': 0,
                                   'avg_value': 0,
                                   'top_societe_name': 'N/A',
                                   'top_societe_percent': 0
                               },
                               monthly_data=[],
                               product_data=[],
                               societe_labels=[],
                               societe_pourcentages=[],
                               societe_destination_datasets=[],
                               produit_societe_datasets=[],
                               produit_destination_datasets=[],
                               produits=[],
                               cramers_v=None,
                               cramers_v_monthly=None,
                               cramers_v_societe_destination=None,
                               cramers_v_produit_destination=None,
                               available_months=[],
                               selected_month=None,
                               search_query='',
                               total_count=0,
                               offset=0,
                               table_html='')

@app.route('/search_invoices', methods=['GET'])
def search_invoices():
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour chercher une facture', 'danger')
        return redirect(url_for('login'))
    search_query = request.args.get('q', '')
    selected_month = request.args.get('month', '')
    offset = request.args.get('offset', type=int, default=0)
    data = get_dashboard_data(offset=offset, search_query=search_query, selected_month=selected_month)
    table_html = get_invoices_table(offset=offset, search_query=search_query, selected_month=selected_month)
    user_id = session.get('user_id', 'Unknown')
    logger.info(f"User ID {user_id} searched invoices with query: {search_query}, month: {selected_month}")
    return render_template('dashboard.html', **data, table_html=table_html)

@app.route('/get_invoices_table', methods=['GET', 'POST'])
def get_invoices_table_route():
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour acceder au tableau des factures', 'danger')
        return redirect(url_for('login'))
    try:
        offset = int(request.args.get('offset', 0))
        search_query = request.args.get('q', '').strip()
        selected_month = request.args.get('month', '').strip()
        return get_invoices_table(offset=offset, limit=10, search_query=search_query, selected_month=selected_month)
    except ValueError as e:
        logger.error(f"Invalid parameter in get_invoices_table_route: {str(e)}")
        flash('Paramètres invalides', 'danger')
        return render_template('invoices_table.html', invoices=[], offset=0, total_count=0,
                              search_query='', selected_month='')

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour téléverser une facture', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        if 'file' not in request.files:
            logger.warning(f"User {session.get('user_id')} attempted upload without file")
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(url_for('upload'))

        file = request.files['file']
        societe_nom = request.form.get('societe', '').strip()

        if file.filename == '':
            logger.warning(f"User {session.get('user_id')} attempted empty file upload")
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(url_for('upload'))

        if file and allowed_file(file.filename):

            try:
                # 1. Extract OT number from filename
                filename = secure_filename(file.filename)
                try:
                    ot_number = filename.split('-')[1]
                    if not ot_number.isdigit():
                        raise ValueError("OT number must be numeric")
                except (IndexError, ValueError) as e:
                    logger.error(f"Invalid filename format: {filename} - {str(e)}")
                    flash("Format de fichier invalide. Le nom doit contenir le numéro OT (ex: 'Facture-12345-2024.pdf')", 'danger')
                    return redirect(url_for('upload'))

                # 2. Process file content
                file_stream = BytesIO(file.read())
                invoice_data = extract_invoice_data(file_stream)
                file_stream.close()

                # Log extracted data for debugging
                logger.debug(f"Extracted invoice data for OT {ot_number}: {invoice_data}")

                # 3. Map names to IDs or insert if not found
                conn = get_connection()
                cursor = conn.cursor(dictionary=True)  # Ensure dictionary cursor
                societe_id = None
                produit_id = None
                destination_id = None

                # Handle societe
                societe_value = societe_nom if societe_nom else (invoice_data.get('societe') or '')
                if societe_value:
                    cursor.execute("SELECT id FROM societe WHERE nom = %s", (societe_value,))
                    result = cursor.fetchone()
                    if result:
                        societe_id = result['id']
                    else:
                        cursor.execute("INSERT INTO societe (nom) VALUES (%s)", (societe_value,))
                        societe_id = cursor.lastrowid

                # Handle produit
                produit_value = invoice_data.get('produit', '')
                if produit_value:
                    cursor.execute("SELECT id FROM produit WHERE nom = %s", (produit_value,))
                    result = cursor.fetchone()
                    if result:
                        produit_id = result['id']
                    else:
                        cursor.execute("INSERT INTO produit (nom) VALUES (%s)", (produit_value,))
                        produit_id = cursor.lastrowid

                # Handle destination
                destination_value = invoice_data.get('destination', '')
                if destination_value:
                    cursor.execute("SELECT id FROM destination WHERE nom = %s", (destination_value,))
                    result = cursor.fetchone()
                    if result:
                        destination_id = result['id']
                    else:
                        cursor.execute("INSERT INTO destination (nom) VALUES (%s)", (destination_value,))
                        destination_id = cursor.lastrowid

                # 4. Validate required fields
                required_fields = ['ot_number', 'societe_id', 'produit_id', 'destination_id']
                missing_fields = [field for field in required_fields if not locals().get(field)]
                if missing_fields:
                    logger.error(f"Missing fields in invoice {ot_number}: {missing_fields}")
                    flash(f'Champs manquants: {", ".join(missing_fields)}', 'danger')
                    return redirect(url_for('upload'))

                # Use fallback values for quantite and total_usd if missing
                quantite = invoice_data.get('quantite', 0)
                total_usd = invoice_data.get('total_usd', 0)

                if quantite <= 0 or total_usd <= 0:
                    logger.warning(f"Invalid or missing values for OT {ot_number}: quantite={quantite}, total_usd={total_usd}")
                    flash("Quantité ou montant total invalide ou manquant dans le fichier.", 'danger')
                    return redirect(url_for('upload'))
                user_id = session.get('user_id')
                cursor.execute("""
                    INSERT INTO invoices (
                        user_id,  -- add this
                        ot_number, invoice_date, societe_id, produit_id, destination_id,
                        quantite, prix_unitaire, total_usd, fret, total_sans_fret
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)  -- add one more placeholder
                """, (
                    user_id,
                    ot_number,
                    invoice_data.get('invoice_date'),
                    societe_id,
                    produit_id,
                    destination_id,
                    quantite,
                    invoice_data.get('prix_unitaire', 0),
                    total_usd,
                    invoice_data.get('fret', 0),
                    invoice_data.get('total_sans_fret', 0)
                ))

                conn.commit()

                logger.info(f"Invoice {ot_number} uploaded by user {session.get('user_id')}")
                flash('Facture traitée avec succès!', 'success')
                return redirect(url_for('dashboard'))

            except mysql.connector.Error as err:
                if err.errno == 1062:  # Duplicate entry
                    flash(f"L'ordre de transfert {ot_number} existe déjà", 'danger')
                else:
                    logger.error(f"Database error: {str(err)}")
                    flash('Erreur de base de données', 'danger')
                return redirect(url_for('upload'))

            except Exception as e:
                logger.error(f"Unexpected error: {str(e)}")
                flash(f'Erreur inattendue: {str(e)}', 'danger')
                return redirect(url_for('upload'))

            finally:
                if 'cursor' in locals(): cursor.close()
                if 'conn' in locals(): conn.close()

    return render_template('upload.html')



@app.route('/telecharger_excel')
def telecharger_excel():
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour télécharger le fichier excel', 'danger')
        return redirect(url_for('login'))
    selected_month = request.args.get('month', '')
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = """
            SELECT
                i.ot_number,
                i.invoice_date,
                s.nom as societe,
                p.nom as produit,
                d.nom as destination,
                i.quantite,
                i.prix_unitaire,
                i.total_usd,
                i.fret,
                i.total_sans_fret
            FROM invoices i
            LEFT JOIN societe s ON i.societe_id = s.id
            LEFT JOIN produit p ON i.produit_id = p.id
            LEFT JOIN destination d ON i.destination_id = d.id
            WHERE 1=1
        """
        params = []
        if selected_month:
            query += " AND DATE_FORMAT(i.invoice_date, '%Y-%m') = %s"
            params.append(selected_month)

        cursor.execute(query, params)
        invoices = cursor.fetchall()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Factures"
        headers = [
            'N° OT', 'Date de facture', 'Société', 'Produit', 'Destination',
            'Quantité', 'Prix unitaire', 'Total USD', 'Fret', 'Total sans fret'
        ]
        ws.append(headers)
        for invoice in invoices:
            ws.append([
                invoice['ot_number'],
                invoice['invoice_date'],
                invoice['societe'],
                invoice['produit'],
                invoice['destination'],
                invoice['quantite'],
                invoice['prix_unitaire'],
                invoice['total_usd'],
                invoice['fret'],
                invoice['total_sans_fret']
            ])

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        filename = f"factures_{selected_month}.xlsx" if selected_month else "factures.xlsx"
        user_id = session.get('user_id', 'Unknown')
        logger.info(f"User ID {user_id} downloaded Excel file: {filename}")
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        logger.error(f"Error in telecharger_excel: {str(e)}")
        flash(f'Erreur lors du téléchargement : {str(e)}', 'danger')
        return redirect(url_for('dashboard'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    user_id = session.get('user_id')
    if not user_id:
        flash('Veuillez vous connecter pour accéder à votre profil.', 'danger')
        return redirect(url_for('login'))

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        if request.method == 'POST':
            new_username = request.form.get('username')
            new_email = request.form.get('email')
            profile_photo = request.files.get('photo_profil')
            current_password = request.form.get('current_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')

            if not new_username or not new_email:
                logger.warning(f"User ID {user_id} attempted profile update without username or email")
                flash('Le nom d\'utilisateur et l\'email sont requis.', 'danger')
                return redirect(url_for('profile'))

            cursor.execute("SELECT id FROM users WHERE email = %s AND id != %s", (new_email, user_id))
            if cursor.fetchone():
                logger.warning(f"User ID {user_id} attempted to use existing email {new_email}")
                flash('Cet email est déjà utilisé par un autre utilisateur.', 'danger')
                return redirect(url_for('profile'))

            if current_password or new_password or confirm_password:
                if not (current_password and new_password and confirm_password):
                    logger.warning(f"User ID {user_id} provided incomplete password fields")
                    flash('Tous les champs du mot de passe sont requis pour le changement.', 'danger')
                    return redirect(url_for('profile'))

                if new_password != confirm_password:
                    logger.warning(f"User ID {user_id} provided mismatched new passwords")
                    flash('Les nouveaux mots de passe ne correspondent pas.', 'danger')
                    return redirect(url_for('profile'))

                cursor.execute("SELECT password FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                if not user or not bcrypt.check_password_hash(user['password'], current_password):
                    logger.warning(f"User ID {user_id} provided incorrect current password")
                    flash('Mot de passe actuel incorrect.', 'danger')
                    return redirect(url_for('profile'))

                hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
                cursor.execute("UPDATE users SET password = %s WHERE id = %s", (hashed_password, user_id))
                logger.info(f"User ID {user_id} updated password")
                flash('Mot de passe mis à jour avec succès !', 'success')

            photo_path = None
            if profile_photo and allowed_profile_file(profile_photo.filename):
                os.makedirs(app.config['PROFILE_UPLOAD_FOLDER'], exist_ok=True)
                filename = secure_filename(profile_photo.filename)
                photo_path = os.path.join(app.config['PROFILE_UPLOAD_FOLDER'], f"{user_id}_{filename}")
                profile_photo.save(photo_path)
                photo_path = f"uploads/profiles/{user_id}_{filename}"

            update_query = """
                UPDATE users
                SET username = %s, email = %s
                WHERE id = %s
            """
            params = [new_username, new_email, user_id]
            if photo_path:
                update_query = """
                    UPDATE users
                    SET username = %s, email = %s, photo_profil = %s
                    WHERE id = %s
                """
                params = [new_username, new_email, photo_path, user_id]

            cursor.execute(update_query, params)
            conn.commit()
            session['username'] = new_username
            session['photo_profil'] = photo_path or session.get('photo_profil')
            logger.info(f"User ID {user_id} updated profile: username={new_username}, email={new_email}")
            flash('Profil mis à jour avec succès !', 'success')
            return redirect(url_for('profile'))

        cursor.execute("SELECT username, email, photo_profil FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            logger.warning(f"User ID {user_id} not found in database")
            flash('Utilisateur non trouvé dans la base de données.', 'danger')
            return redirect(url_for('login'))

        return render_template('profile.html', user=user)
    except mysql.connector.Error as err:
        logger.error(f"Database error in profile: {str(err)}")
        flash(f'Erreur de base de données : {str(err)}', 'danger')
        return redirect(url_for('dashboard'))
    except Exception as e:
        logger.error(f"Error in profile: {str(e)}")
        flash(f'Erreur : {str(e)}', 'danger')
        return redirect(url_for('dashboard'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()


@app.route('/users')
def user_management():
    if not session.get('is_admin'):
        flash('Accès refusé. Seuls les admins peuvent gérer les utilisateurs.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, username, email, is_admin FROM users ORDER BY id")
        users = cursor.fetchall()
        user_id = session.get('user_id', 'Unknown')
        logger.info(f"User ID {user_id} accessed user management")
        return render_template('users.html', users=users)
    except mysql.connector.Error as err:
        logger.error(f"Database error in user_management: {str(err)}")
        flash(f'Erreur de base de données : {str(err)}', 'danger')
        return redirect(url_for('dashboard'))
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()


@app.route('/delete_account/<int:user_id>', methods=['POST'])
def delete_account(user_id):
    session_id = session.get('user_id')
    is_admin = session.get('is_admin', False)

    if not is_admin:
        logger.warning(f"Non-admin user ID {session_id or 'Unknown'} attempted to delete user ID {user_id}")
        flash('Accès refusé. Seuls les admins peuvent supprimer des comptes.', 'danger')
        return redirect(url_for('user_management'))

    if user_id == session_id:
        logger.warning(f"Admin user ID {user_id} attempted to delete their own account")
        flash('Vous ne pouvez pas supprimer votre propre compte.', 'danger')
        return redirect(url_for('user_management'))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        # Vérifier si l'utilisateur existe
        cursor.execute("SELECT id, is_admin FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            flash('Utilisateur introuvable.', 'warning')
            return redirect(url_for('user_management'))

        # Vérifier si l'utilisateur est un admin et s'il est le dernier admin restant
        if user['is_admin']:
            cursor.execute("SELECT COUNT(*) AS total_admins FROM users WHERE is_admin = TRUE")
            result = cursor.fetchone()
            if result['total_admins'] <= 1:
                flash("Impossible de supprimer cet administrateur : l'application doit contenir au moins un admin.", "warning")
                return redirect(url_for('user_management'))

        # Vérifier si l'utilisateur est lié à des factures
        cursor.execute("SELECT COUNT(*) AS count FROM invoices WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        if result['count'] > 0:
            flash("Impossible de supprimer cet utilisateur car il est lié à des factures existantes.", "warning")
            return redirect(url_for('user_management'))

        # Supprimer l'utilisateur
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        logger.info(f"Deleted user ID {user_id}")
        flash('Compte supprimé avec succès.', 'success')
        return redirect(url_for('user_management'))

    except mysql.connector.Error as err:
        if conn and conn.in_transaction:
            conn.rollback()
        logger.error(f"Database error during deletion of user ID {user_id}: {str(err)}")
        flash(f'Erreur de suppression : {str(err)}', 'danger')
        return redirect(url_for('user_management'))

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()



@app.route('/delete_invoice/<string:ot_number>', methods=['POST'])
def delete_invoice(ot_number):
    is_admin = session.get('is_admin', False)
    if not is_admin:
        user_id = session.get('user_id', 'Unknown')
        logger.warning(f"Non-admin user ID {user_id} attempted to delete invoice {ot_number}")
        return jsonify({"success": False, "message": "Accès refusé. Seuls les admins peuvent supprimer des factures."}), 403

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        if conn.in_transaction:
            conn.rollback()
            logger.warning(f"Rolled back existing transaction before deleting invoice {ot_number}")

        conn.start_transaction()

        cursor.execute("SELECT ot_number FROM invoices WHERE ot_number = %s", (ot_number,))
        if not cursor.fetchone():
            user_id = session.get('user_id', 'Unknown')
            logger.warning(f"User ID {user_id} attempted to delete non-existent invoice {ot_number}")
            return jsonify({"success": False, "message": f"L'ordre de transfert n° {ot_number} n'existe pas."}), 404

        cursor.execute("DELETE FROM invoices WHERE ot_number = %s", (ot_number,))
        conn.commit()

        user_id = session.get('user_id', 'Unknown')
        logger.info(f"User ID {user_id} deleted invoice {ot_number}")
        return jsonify({"success": True, "message": f"Facture n° {ot_number} supprimée avec succès."})

    except mysql.connector.Error as err:
        if conn and conn.in_transaction:
            conn.rollback()
        user_id = session.get('user_id', 'Unknown')
        logger.error(f"Database error during deletion of invoice {ot_number} by User ID {user_id}: {str(err)}")
        return jsonify({"success": False, "message": f"Erreur de suppression: {str(err)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


@app.route('/manuel_insertion', methods=['GET','POST'])
def manuel_insertion():
    # Check if user is logged in
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour insérer une facture manuellement', 'danger')
        return redirect(url_for('login'))

    logger.info(f"User ID {session.get('user_id')} accessed /manuel_insertion")

    # Fetch options from database
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT nom FROM destination ORDER BY nom")
        destinations = [row['nom'] for row in cursor.fetchall()]

        cursor.execute("SELECT nom FROM societe ORDER BY nom")
        societes = [row['nom'] for row in cursor.fetchall()]

        cursor.execute("SELECT nom FROM produit ORDER BY nom")
        produits = [row['nom'] for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error fetching options in manuel_insertion for user {session.get('user_id')}: {str(e)}")
        flash('Erreur lors de la récupération des options', 'danger')
        return render_template('manuel_insertion.html')
    finally:
        cursor.close()
        conn.close()

    if request.method == 'POST':
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            # Extract and sanitize form data
            ot_number = request.form.get('nombre', '').strip()
            invoice_date = request.form.get('date', '').strip()
            destination = request.form.get('destination', '').strip()
            societe = request.form.get('societe', '').strip()
            produit = request.form.get('produit', '').strip()
            quantite = request.form.get('quantite', '').strip()
            prix_unitaire = request.form.get('prix_unitaire', '').strip()
            total_usd = request.form.get('total_usd', '').strip()
            fret = request.form.get('fret', '').strip()

            # Validate all required fields are present
            if not all([ot_number, invoice_date, destination, societe, produit, quantite, prix_unitaire, total_usd]):
                logger.warning(f"User {session.get('user_id')} submitted incomplete form")
                flash('Tous les champs obligatoires doivent être remplis.', 'danger')
                return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)

            # Validate numeric fields
            try:
                quantite = float(quantite)
                prix_unitaire = float(prix_unitaire)
                total_usd = float(total_usd)
                fret = float(fret) if fret else 0.0
                if quantite <= 0 or prix_unitaire < 0 or total_usd < 0 or fret < 0:
                    logger.warning(f"User {session.get('user_id')} submitted invalid numeric values: quantite={quantite}, prix_unitaire={prix_unitaire}, total_usd={total_usd}, fret={fret}")
                    flash('Les valeurs numériques doivent être positives.', 'danger')
                    return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)
            except ValueError:
                logger.warning(f"User {session.get('user_id')} submitted non-numeric values")
                flash('Les champs quantité, prix unitaire, total USD et fret doivent être des nombres.', 'danger')
                return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)

            # Validate date format
            try:
                invoice_date = datetime.strptime(invoice_date, '%Y-%m-%d').date()
            except ValueError:
                logger.warning(f"User {session.get('user_id')} submitted invalid date format: {invoice_date}")
                flash('La date doit être au format AAAA-MM-JJ.', 'danger')
                return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)

            # Calculate total_sans_fret
            total_sans_fret = round(total_usd - (fret * quantite), 2) if fret else total_usd

            # Check for duplicate OT number
            cursor.execute('SELECT ot_number FROM invoices WHERE ot_number = %s', (ot_number,))
            if cursor.fetchone():
                logger.warning(f"User {session.get('user_id')} attempted to insert duplicate OT {ot_number}")
                flash(f"L'ordre de transfert n° {ot_number} existe déjà.", 'danger')
                return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)

            # Map names to IDs or insert if not found
            cursor.execute('SELECT id FROM societe WHERE nom = %s', (societe,))
            result = cursor.fetchone()
            societe_id = result['id'] if result else None
            if not societe_id:
                cursor.execute('INSERT INTO societe (nom) VALUES (%s)', (societe,))
                societe_id = cursor.lastrowid

            cursor.execute('SELECT id FROM produit WHERE nom = %s', (produit,))
            result = cursor.fetchone()
            produit_id = result['id'] if result else None
            if not produit_id:
                cursor.execute('INSERT INTO produit (nom) VALUES (%s)', (produit,))
                produit_id = cursor.lastrowid

            cursor.execute('SELECT id FROM destination WHERE nom = %s', (destination,))
            result = cursor.fetchone()
            destination_id = result['id'] if result else None
            if not destination_id:
                cursor.execute('INSERT INTO destination (nom) VALUES (%s)', (destination,))
                destination_id = cursor.lastrowid

            # Validate required IDs
            if not all([societe_id, produit_id, destination_id]):
                missing = [field for field, value in [('societe_id', societe_id), ('produit_id', produit_id), ('destination_id', destination_id)] if not value]
                logger.error(f"Missing IDs for invoice {ot_number} by user {session.get('user_id')}: {missing}")
                flash(f'Champs manquants: {", ".join(missing)}', 'danger')
                return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)

            user_id = session.get('user_id')  # Récupère l'ID de l'utilisateur connecté

            query = """
                INSERT INTO invoices (
                    ot_number, invoice_date, societe_id, produit_id, destination_id,
                    quantite, prix_unitaire, total_usd, fret, total_sans_fret, user_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            values = (
                ot_number, invoice_date, societe_id, produit_id, destination_id,
                quantite, prix_unitaire, total_usd, fret, total_sans_fret, user_id
            )

            cursor.execute(query, values)
            conn.commit()
            logger.info(f"User {session.get('user_id')} manually inserted invoice {ot_number}")
            flash('Facture insérée avec succès !', 'success')
            return redirect(url_for('dashboard'))

        except mysql.connector.Error as err:
            if err.errno == 1062:
                flash(f"L'ordre de transfert n° {ot_number} existe déjà.", 'danger')
                logger.warning(f"Duplicate invoice OT number {ot_number} by user {session.get('user_id')}")
            else:
                logger.error(f"Database error in manuel_insertion for user {session.get('user_id')}: {str(err)}")
                flash('Erreur de base de données', 'danger')
            return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)
        except Exception as e:
            logger.error(f"Unexpected error in manuel_insertion for user {session.get('user_id')}: {str(e)}")
            flash('Erreur inattendue', 'danger')
            return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)
        finally:
            cursor.close()
            conn.close()

    return render_template('manuel_insertion.html', destinations=destinations, societes=societes, produits=produits)

@app.route('/edit_entities', methods=['GET', 'POST'])
def edit_entities():
    if not session.get('user_id'):
        flash('Veuillez vous connecter pour accéder à cette page', 'danger')
        return redirect(url_for('login'))
    if not session.get('is_admin'):
        flash('Accès refusé. Cette page est réservée aux administrateurs.', 'danger')
        return redirect(url_for('dashboard'))

    logger.info(f"Admin User ID {session.get('user_id')} accessed /edit_entities")

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Read all entities
        cursor.execute("SELECT id, nom FROM societe ORDER BY nom")
        societes = cursor.fetchall()

        cursor.execute("SELECT id, nom FROM produit ORDER BY nom")
        produits = cursor.fetchall()

        cursor.execute("SELECT id, nom FROM destination ORDER BY nom")
        destinations = cursor.fetchall()

        if request.method == 'POST':
            action = request.form.get('action')
            entity_type = request.form.get('entity_type')
            entity_id = request.form.get('id', '').strip()
            entity_nom = request.form.get('nom', '').strip()
            if action == 'create':
                if not entity_nom:
                    flash('Le nom ne peut pas être vide.', 'danger')
                else:
                    table = {'societe': 'societe', 'produit': 'produit', 'destination': 'destination'}.get(entity_type)
                    if table:
                        cursor.execute(f"INSERT INTO {table} (nom) VALUES (%s)", (entity_nom,))
                        conn.commit()
                        logger.info(f"Admin {session.get('user_id')} created {entity_type} with name {entity_nom}")
                        flash(f'{entity_type.capitalize()} créé avec succès !', 'success')
                    else:
                        flash('Type d\'entité invalide.', 'danger')

            elif action == 'update':
                if not entity_id or not entity_nom:
                    flash('L\'ID et le nom sont requis pour la mise à jour.', 'danger')
                else:
                    table = {'societe': 'societe', 'produit': 'produit', 'destination': 'destination'}.get(entity_type)
                    if table:
                        cursor.execute(f"UPDATE {table} SET nom = %s WHERE id = %s", (entity_nom, entity_id))
                        conn.commit()
                        logger.info(f"Admin {session.get('user_id')} updated {entity_type} ID {entity_id} to {entity_nom}")
                        flash(f'{entity_type.capitalize()} mis à jour avec succès !', 'success')
                    else:
                        flash('Type d\'entité invalide.', 'danger')

            elif action == 'delete':
                if not entity_id:
                    flash('L\'ID est requis pour la suppression.', 'danger')
                else:
                    table = {'societe': 'societe', 'produit': 'produit', 'destination': 'destination'}.get(entity_type)
                    if table:
                        cursor.execute(f"DELETE FROM {table} WHERE id = %s", (entity_id,))
                        conn.commit()
                        logger.info(f"Admin {session.get('user_id')} deleted {entity_type} ID {entity_id}")
                        flash(f'{entity_type.capitalize()} supprimé avec succès !', 'success')
                    else:
                        flash('Type d\'entité invalide.', 'danger')

            return redirect(url_for('edit_entities'))

        return render_template('edit_entities.html', societes=societes, produits=produits, destinations=destinations)

    except mysql.connector.Error as err:
        logger.error(f"Database error in edit_entities for user {session.get('user_id')}: {str(err)}")
        flash('Erreur de base de données', 'danger')
        return render_template('edit_entities.html', societes=[], produits=[], destinations=[])
    finally:
        cursor.close()
        conn.close()


@app.route('/logout')
def logout():
    user_id = session.get('user_id', 'Unknown')
    session.clear()
    logger.info(f"User ID {user_id} logged out")
    flash('Vous avez été déconnecté.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)