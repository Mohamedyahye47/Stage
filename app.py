from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session, jsonify
from functools import wraps
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
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_wtf import CSRFProtect
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bcrypt = Bcrypt(app)
#csrf = CSRFProtect(app)  # Commented out to disable CSRF
app.config['WTF_CSRF_ENABLED'] = False  # Disable CSRF protection for now

# Security: Replace with a secure random key in production
app.secret_key = os.environ.get('FLASK_SECRET', os.urandom(24))
app.config['UPLOAD_FOLDER'] = 'Uploads'
app.config['PROFILE_UPLOAD_FOLDER'] = 'static/uploads/profiles'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}
app.config['ALLOWED_PROFILE_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB per file

# Flask-Login setup
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# User class
class User(UserMixin):
    def __init__(self, id, is_admin=False):
        self.id = id
        self.username = None
        self.photo_profil = None
        self._is_admin = is_admin

    @property
    def is_admin(self):
        return self._is_admin

@login_manager.user_loader
def load_user(user_id):
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, username, photo_profil 
            FROM users WHERE id = %s
        """, (user_id,))
        user_data = cursor.fetchone()

        if user_data:
            user = User(user_data['id'], is_admin=(user_data['id'] == 2))
            user.username = user_data['username']
            user.photo_profil = user_data['photo_profil']
            logger.info(f"Loaded user {user_id} with is_admin={user.is_admin}")  # Debug log
            return user
        return None
    except mysql.connector.Error as err:
        logger.error(f"Database error in load_user: {err}")
        return None
    finally:
        cursor.close()
        conn.close()

@app.route('/users')
@login_required
def user_management():
    if current_user.id != 2:  # Use ID check instead of is_admin
        logger.warning(f"Non-admin user ID {current_user.id} attempted to access user_management")
        flash('Accès refusé. Seule l\'admin peut gérer les utilisateurs.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, username, email FROM users ORDER BY id")
        users = cursor.fetchall()
        logger.info(f"User ID {current_user.id} accessed user management")
        return render_template('users.html', users=users)
    except mysql.connector.Error as err:
        logger.error(f"Database error in user_management: {str(err)}")
        flash(f'Erreur de base de données : {str(err)}', 'danger')
        return redirect(url_for('dashboard'))
    finally:
        cursor.close()
        conn.close()

@app.route('/delete_account/<int:user_id>', methods=['POST'])
@login_required
def delete_account(user_id):
    if current_user.id != 2:  # Use ID check instead of is_admin
        logger.warning(f"Non-admin user ID {current_user.id} attempted to delete user ID {user_id}")
        flash('Accès refusé. Seule l\'admin peut supprimer des comptes.', 'danger')
        return redirect(url_for('dashboard'))

    if user_id == current_user.id:
        logger.warning(f"Admin user ID {user_id} attempted to delete their own account")
        flash('Vous ne pouvez pas supprimer votre propre compte.', 'danger')
        return redirect(url_for('user_management'))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Check if a transaction is already in progress and rollback if necessary
        if conn.in_transaction:
            conn.rollback()
            logger.warning(f"Rolled back existing transaction before starting new one for user ID {user_id}")

        # Start a new transaction
        conn.start_transaction()

        # Verify user exists
        cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            logger.warning(f"User ID {user_id} not found")
            flash('Utilisateur introuvable.', 'warning')
            conn.rollback()
            return redirect(url_for('user_management'))

        # Delete the user (invoices remain unaffected)
        cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
        logger.info(f"Deleted user ID {user_id}")

        conn.commit()
        flash('Compte supprimé avec succès.', 'success')
        return redirect(url_for('user_management'))

    except mysql.connector.Error as err:
        if conn and conn.in_transaction:
            conn.rollback()
        logger.error(f"Database error during deletion of user ID {user_id}: {str(err)}")
        flash(f'Erreur de suppression: {str(err)}', 'danger')
        return redirect(url_for('user_management'))
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Database configuration
db_config = {
    'host': os.environ.get('MYSQL_HOST', 'localhost'),
    'user': os.environ.get('MYSQL_USER', 'root'),
    'password': os.environ.get('MYSQL_PASSWORD', 'MedYahya47!!'),
    'database': os.environ.get('MYSQL_DB', 'data_upload')
}


# Custom filter for number formatting (French format)
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
        return mysql.connector.connect(**db_config)
    except mysql.connector.Error as err:
        logger.error(f"Database connection error: {str(err)}")
        flash(f'Erreur de connexion à la base de données : {str(err)}', 'danger')
        raise

# Context processor to inject user data into all templates
@app.context_processor
def inject_user():
    if current_user.is_authenticated:
        return {
            'current_user': {
                'id': current_user.id,
                'username': getattr(current_user, 'username', None),
                'photo_profil': getattr(current_user, 'photo_profil', None),
                'is_authenticated': True
            }
        }
    return {'current_user': None}

# Rest of the code (e.g., extract_invoice_data, get_dashboard_data, etc.) remains unchanged
def extract_invoice_data(pdf_path):
    text = ""
    with open(pdf_path, 'rb') as file:
        pdf_reader = PyPDF2.PdfReader(file)
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"

    def extract_ot_number(text):
        patterns = [
            r'Ordre\s+de\s+transfert\s*No\s*[:=-]?\s*([A-Z]?\d{4,})',
            r'OT\s*[:]?\s*(\d{4,})',
            r'N°\s*Ordre\s*:\s*(\d{4,})',
            r'Addax\s+ref\.\s*(\d{4,})',
            r'FACTURE\s+COMMERCIALE\s*No\s+[A-Z]?(\d{4,})'
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                ot_num = re.sub(r'[^0-9]', '', match.group(1))
                return ot_num.zfill(5) if len(ot_num) >= 4 else None
        return None

    company_patterns = [
        r'STAR OIL MAURITANIE',
        r'RIM OIL',
        r'RIMACO',
        r'M2P OIL SA',
        r'SEMP SA',
        r'SEBKHA - PLAGE DES PECHEURS',
        r'LEADER PETROLEUM',
        r'Contrepartie\s*:\s*([^\n]+)',
        r'Contrepartie\s+([^\n]+)',
        r'^([A-Z0-9][A-Z0-9& ]+[A-Z0-9])\s*[\r\n]+(?:BP|\d|SEBKHA|Z ART)'
    ]
    societe = None
    for pattern in company_patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            societe = match.group(1) if len(match.groups()) > 0 else match.group(0)
            societe = societe.strip()
            if "Contrepartie" in societe:
                societe = societe.replace("Contrepartie", "").strip()
            break

    product_match = re.search(r'PRODUIT\s*\|\s*([^\|]+)', text)
    if not product_match:
        product_match = re.search(r'Produit:\s*([^\n]+)', text)
    produit = product_match.group(1).strip() if product_match else None

    quantite_match = re.search(
        r'(?:QUANTITE\s*\|\s*|Quantité \(Tonnes Métriques\)\s*|Quantity:\s*)([\d\',\.]+)\s*(?:MT|Tonnes|TM)?',
        text, re.IGNORECASE
    )
    quantite = None
    if quantite_match:
        try:
            quantite_str = quantite_match.group(1)
            quantite = float(quantite_str.replace("'", "").replace(",", "").replace(" ", ""))
        except (ValueError, AttributeError):
            quantite = None

    total_usd_match = re.search(r'Montant total de la facture\s*\$([\d\',]+\.\d{2})', text)
    total_usd = float(total_usd_match.group(1).replace("'", "").replace(",", "")) if total_usd_match else None

    fret_match = re.search(r'FRET USD / Tonne Métrique\s*\$([\d\.,]+)', text)
    fret = float(fret_match.group(1).replace(",", "")) if fret_match else None

    total_sans_fret = round(total_usd - (fret * quantite), 2) if total_usd and fret and quantite else None

    invoice_date = None
    try:
        date_match = re.search(r'Date du Bordereau de cession en bac:\s*(\d{2}\.\d{2}\.\d{4})', text)
        if date_match:
            date_str = date_match.group(1)
            if isinstance(date_str, str):
                invoice_date = datetime.strptime(date_str, '%d.%m.%Y').date()
        else:
            date_match_alt = re.search(r'Date du Bordereau de cession en bac:\s*(\d{2}/\d{2}/\d{4})', text)
            if date_match_alt:
                date_str_alt = date_match_alt.group(1)
                if isinstance(date_str_alt, str):
                    invoice_date = datetime.strptime(date_str_alt, '%d/%m/%Y').date()
    except (ValueError, AttributeError, TypeError) as e:
        logger.error(f"Error parsing date: {e}")

    data = {
        'ot_number': extract_ot_number(text),
        'invoice_date': invoice_date,
        'destination': re.search(r'Terminal:\s*([^\n]+)', text).group(1).split()[0] if re.search(
            r'Terminal:\s*([^\n]+)', text) else None,
        'societe': societe,
        'produit': produit,
        'quantite': quantite or 0,
        'prix_unitaire': float(
            re.search(r'Prix Unitaire\s+\$([\d\',]+\.\d{2})', text).group(1).replace("'", "").replace(",", "")
        ) if re.search(r'Prix Unitaire\s+\$([\d\',]+\.\d{2})', text) else None,
        'total_usd': total_usd or 0,
        'fret': fret,
        'total_sans_fret': total_sans_fret
    }
    return data

def get_dashboard_data(offset=0, limit=10, search_query=None, selected_month=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get available months for the dropdown
        cursor.execute("""
            SELECT DISTINCT DATE_FORMAT(invoice_date, '%Y-%m') as month
            FROM invoices
            WHERE invoice_date IS NOT NULL
            ORDER BY month DESC
        """)
        available_months = [row['month'] for row in cursor.fetchall()]

        # Base WHERE clause for all queries
        where_clause = " WHERE 1=1"
        params = []
        if selected_month:
            where_clause += " AND DATE_FORMAT(invoice_date, '%Y-%m') = %s"
            params.append(selected_month)
        if search_query and search_query.strip():
            search_query = f"%{search_query}%"
            where_clause += " AND (ot_number LIKE %s OR societe LIKE %s OR produit LIKE %s OR invoice_date LIKE %s)"
            params.extend([search_query, search_query, search_query, search_query])

        # Get invoices with pagination
        query = f"""
            SELECT 
                ot_number,
                DATE_FORMAT(invoice_date, '%Y-%m-%d') as invoice_date,
                societe,
                produit,
                COALESCE(quantite, 0) as quantite,
                COALESCE(total_usd, 0) as total_usd,
                total_sans_fret
            FROM invoices 
            {where_clause}
            ORDER BY created_at DESC 
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        cursor.execute(query, params)
        invoices = cursor.fetchall()

        # Get total count for pagination
        cursor.execute(f"""
            SELECT COUNT(*) as total_count
            FROM invoices
            {where_clause}
        """, params[:-2])
        total_count = cursor.fetchone()['total_count']

        # Get summary statistics
        cursor.execute(f"""
            SELECT 
                COUNT(*) as total_invoices,
                COALESCE(SUM(total_usd), 0) as total_value,
                COALESCE(AVG(total_usd), 0) as avg_value
            FROM invoices
            {where_clause}
        """, params[:-2])
        stats = cursor.fetchone()

        # Calculate top société by USD value
        cursor.execute(f"""
            SELECT 
                societe,
                COALESCE(SUM(total_usd), 0) as total_usd,
                (COALESCE(SUM(total_usd), 0) / NULLIF((SELECT SUM(total_usd) FROM invoices {where_clause}), 0) * 100) as percentage
            FROM invoices
            {where_clause}
            GROUP BY societe
            ORDER BY total_usd DESC
            LIMIT 1
        """, params[:-2] + params[:-2])
        top_societe = cursor.fetchone()
        stats['top_societe_name'] = top_societe['societe'] if top_societe else 'N/A'
        stats['top_societe_percent'] = round(top_societe['percentage'], 1) if top_societe else 0

        # Monthly totals for chart
        cursor.execute("""
            SELECT 
                DATE_FORMAT(invoice_date, '%Y-%m') as month,
                COALESCE(SUM(total_usd), 0) as total
            FROM invoices
            GROUP BY month
            ORDER BY month
        """)
        monthly_data = cursor.fetchall()

        # Calculate Cramér's V for monthly data (month vs société)
        cursor.execute(f"""
            SELECT 
                DATE_FORMAT(invoice_date, '%Y-%m') as month,
                societe,
                COALESCE(SUM(total_usd), 0) as total
            FROM invoices
            {where_clause}
            GROUP BY month, societe
        """, params[:-2])
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
                    cramers_v_monthly = np.sqrt(phi2 / min((k-1), (r-1)))
            except ValueError:
                cramers_v_monthly = None

        # Product distribution (percentages by quantity)
        cursor.execute(f"""
            SELECT 
                COALESCE(produit, 'Inconnu') as produit,
                COALESCE(SUM(quantite), 0) as total_quantite,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            {where_clause}
            GROUP BY produit
        """, params[:-2])
        product_data_raw = cursor.fetchall()
        total_quantite = sum(row['total_quantite'] for row in product_data_raw) or 1
        product_data = [{
            'produit': row['produit'],
            'total_quantite': row['total_quantite'],
            'total_usd': row['total_usd'],
            'percentage': round((row['total_quantite'] / total_quantite) * 100, 1)
        } for row in product_data_raw]

        # Company data (percentages by quantity)
        cursor.execute(f"""
            SELECT 
                COALESCE(societe, 'Inconnu') as societe,
                COALESCE(SUM(quantite), 0) as total_quantite,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            {where_clause}
            GROUP BY societe
        """, params[:-2])
        societe_data_raw = cursor.fetchall()
        total_quantite_all = sum(row['total_quantite'] for row in societe_data_raw) or 1
        societe_labels = [row['societe'] for row in societe_data_raw]
        societe_pourcentages = [
            round((row['total_quantite'] / total_quantite_all) * 100, 1) for row in societe_data_raw
        ]

        # Société/Destination data (percentages per société)
        cursor.execute(f"""
            SELECT 
                COALESCE(societe, 'Inconnu') as societe,
                COALESCE(destination, 'Inconnu') as destination,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            {where_clause}
            GROUP BY societe, destination
        """, params[:-2])
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
                        cramers_v_societe_destination = np.sqrt(phi2 / min((k-1), (r-1)))
                except ValueError:
                    cramers_v_societe_destination = None

        # Produit/Destination data (percentages per produit)
        cursor.execute(f"""
            SELECT 
                COALESCE(produit, 'Inconnu') as produit,
                COALESCE(destination, 'Inconnu') as destination,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            {where_clause}
            GROUP BY produit, destination
            HAVING SUM(total_usd) > 0
        """, params[:-2])
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
                        cramers_v_produit_destination = np.sqrt(phi2 / min((k-1), (r-1)))
                except ValueError:
                    cramers_v_produit_destination = None

        # Product vs Société data (percentages per société)
        cursor.execute(f"""
            SELECT 
                COALESCE(societe, 'Inconnu') as societe,
                COALESCE(produit, 'Inconnu') as produit,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            {where_clause}
            GROUP BY societe, produit
        """, params[:-2])
        product_societe_data = cursor.fetchall()
        produit_societe_datasets = []
        cramers_v = None

        if product_societe_data and len(product_societe_data) >= 2:
            df_ps = pd.DataFrame(product_societe_data)
            produits = sorted(df_ps['produit'].unique())
            try:
                if len(df_ps['societe'].unique()) > 1 and len(produits) > 1:
                    contingency_table = pd.crosstab(df_ps['societe'], df_ps['produit'])
                    chi2, _, _, _ = chi2_contingency(contingency_table)
                    n = contingency_table.sum().sum()
                    phi2 = chi2 / n
                    r, k = contingency_table.shape
                    cramers_v = np.sqrt(phi2 / min((k-1), (r-1)))
            except ValueError:
                cramers_v = None

            colors = ['#4C78A8', '#F58518', '#E45756', '#72B7B2']
            societe_totals = df_ps.groupby('societe')['total_usd'].sum().to_dict()
            for i, produit in enumerate(produits):
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

    finally:
        cursor.close()
        conn.close()

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
        'produits': produits or [],  # Added for produit_destination_chart labels
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

def get_invoices_table(offset=0, limit=10, search_query=None, selected_month=None):
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        where_clause = " WHERE 1=1"
        params = []
        if selected_month:
            where_clause += " AND DATE_FORMAT(invoice_date, '%Y-%m') = %s"
            params.append(selected_month)
        if search_query and search_query.strip():
            search_query = f"%{search_query}%"
            where_clause += " AND (ot_number LIKE %s OR societe LIKE %s OR produit LIKE %s OR invoice_date LIKE %s)"
            params.extend([search_query, search_query, search_query, search_query])

        query = f"""
            SELECT 
                ot_number,
                DATE_FORMAT(invoice_date, '%Y-%m-%d') as invoice_date,
                societe,
                produit,
                COALESCE(quantite, 0) as quantite,
                COALESCE(total_usd, 0) as total_usd,
                total_sans_fret
            FROM invoices 
            {where_clause}
            ORDER BY created_at DESC 
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        cursor.execute(query, params)
        invoices = cursor.fetchall()

        cursor.execute(f"""
            SELECT COUNT(*) as total_count
            FROM invoices
            {where_clause}
        """, params[:-2])
        total_count = cursor.fetchone()['total_count']

        return render_template('invoices_table.html', invoices=invoices, offset=offset, total_count=total_count, search_query=search_query, selected_month=selected_month)
    finally:
        cursor.close()
        conn.close()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Veuillez vous connecter d\'abord.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Veuillez vous connecter d\'abord.', 'danger')
            return redirect(url_for('login'))
        if current_user.id != 2:  # Use ID check instead of is_admin
            flash('Accès refusé. Seule l\'admin est autorisé.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/')
def home():
    return redirect(url_for('login'))

@app.route('/login', methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        email = request.form["email"]
        password = request.form['password']

        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT id, password FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if user and bcrypt.check_password_hash(user['password'], password):
                session['user_id'] = user['id']
                login_user(User(user['id'], is_admin=(user['id'] == 2)))  # Explicitly set is_admin
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
            cursor.close()
            conn.close()

    return render_template('login.html')

@app.route('/register', methods=["GET", "POST"])
@admin_required
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = bcrypt.generate_password_hash(request.form['password']).decode('utf-8')

        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)

            # Check if email exists
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                logger.warning(f"Registration failed: Email {email} already exists")
                flash('Cet email est déjà utilisé', 'danger')
                return redirect(url_for('register'))

            # Insert new user
            cursor.execute(
                "INSERT INTO users (username, email, password) VALUES (%s, %s, %s)",
                (username, email, password)
            )
            conn.commit()
            logger.info(f"New user registered: {username} (email: {email})")
            flash('Compte créé avec succès !', 'success')
            return redirect(url_for('dashboard'))
        except mysql.connector.Error as err:
            logger.error(f"Database error in register: {str(err)}")
            flash(f'Erreur de base de données : {str(err)}', 'danger')
            return redirect(url_for('register'))
        finally:
            cursor.close()
            conn.close()

    return render_template('register.html')

@app.route('/dashboard')
@login_required
def dashboard():
    try:
        search_query = request.args.get('q', '')
        selected_month = request.args.get('month', '')
        offset = request.args.get('offset', type=int, default=0)
        data = get_dashboard_data(offset=offset, search_query=search_query, selected_month=selected_month)
        table_html = get_invoices_table(offset=offset, search_query=search_query, selected_month=selected_month)
        logger.info(f"User ID {current_user.id} accessed dashboard")
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
                               cramers_v=None,
                               cramers_v_monthly=None,
                               cramers_v_societe_destination=None,
                               available_months=[],
                               selected_month=None,
                               search_query='',
                               total_count=0,
                               offset=0,
                               table_html='')
    finally:
        if 'conn' in locals():
            conn.close()

@app.route('/search_invoices', methods=['GET'])
@login_required
def search_invoices():
    search_query = request.args.get('q', '')
    selected_month = request.args.get('month', '')
    offset = request.args.get('offset', type=int, default=0)
    data = get_dashboard_data(offset=offset, search_query=search_query, selected_month=selected_month)
    table_html = get_invoices_table(offset=offset, search_query=search_query, selected_month=selected_month)
    logger.info(f"User ID {current_user.id} searched invoices with query: {search_query}, month: {selected_month}")
    return render_template('dashboard.html', **data, table_html=table_html)

@app.route('/get_invoices_table')
@login_required
def get_invoices_table_route():
    offset = request.args.get('offset', type=int, default=0)
    search_query = request.args.get('q', '')
    selected_month = request.args.get('month', '')
    logger.info(f"User ID {current_user.id} requested invoices table with offset {offset}")
    return get_invoices_table(offset=offset, search_query=search_query, selected_month=selected_month)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            logger.warning(f"User ID {current_user.id} attempted to upload without selecting a file")
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(request.url)

        file = request.files['file']
        societe = request.form.get('societe')

        if file.filename == '':
            logger.warning(f"User ID {current_user.id} attempted to upload an empty file")
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(request.url)

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            try:
                invoice_data = extract_invoice_data(filepath)
                invoice_data['societe'] = societe or invoice_data['societe']
                invoice_date = invoice_data['invoice_date']

                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO invoices (
                        ot_number, invoice_date, destination, societe, produit,
                        quantite, prix_unitaire, total_usd, fret, total_sans_fret
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    invoice_data['ot_number'], invoice_date, invoice_data['destination'],
                    invoice_data['societe'], invoice_data['produit'], invoice_data['quantite'],
                    invoice_data['prix_unitaire'], invoice_data['total_usd'], invoice_data['fret'],
                    invoice_data['total_sans_fret']
                ))
                conn.commit()
                logger.info(f"User ID {current_user.id} uploaded invoice {invoice_data['ot_number']}")
                flash('Facture traitée avec succès !', 'success')
                return redirect(url_for('dashboard'))
            except mysql.connector.Error as err:
                if err.errno == 1062:
                    logger.warning(f"Duplicate invoice OT number {invoice_data['ot_number']} by user ID {current_user.id}")
                    flash(f"Erreur : L'ordre de transfert n° {invoice_data['ot_number']} existe déjà", 'danger')
                else:
                    logger.error(f"Database error in upload: {str(err)}")
                    flash(f'Erreur de base de données : {str(err)}', 'danger')
                return redirect(request.url)
            except Exception as e:
                logger.error(f"Error processing invoice upload: {str(e)}")
                flash(f'Erreur lors du traitement de la facture : {str(e)}', 'danger')
                return redirect(request.url)
            finally:
                cursor.close()
                conn.close()
    return render_template('upload.html')

@app.route('/telecharger_excel')
@login_required
def telecharger_excel():
    selected_month = request.args.get('month', '')
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = """
            SELECT 
                ot_number,
                invoice_date,
                destination,
                societe,
                produit,
                quantite,
                prix_unitaire,
                total_usd,
                fret,
                total_sans_fret
            FROM invoices
            WHERE 1=1
        """
        params = []
        if selected_month:
            query += " AND DATE_FORMAT(invoice_date, '%Y-%m') = %s"
            params.append(selected_month)

        cursor.execute(query, params)
        invoices = cursor.fetchall()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Factures"
        headers = [
            'N° OT', 'Date de facture', 'Destination', 'Société',
            'Produit', 'Quantité', 'Prix unitaire', 'Total USD',
            'Fret', 'Total sans fret'
        ]
        ws.append(headers)
        for invoice in invoices:
            ws.append([
                invoice['ot_number'],
                invoice['invoice_date'],
                invoice['destination'],
                invoice['societe'],
                invoice['produit'],
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
        logger.info(f"User ID {current_user.id} downloaded Excel file: {filename}")
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
        cursor.close()
        conn.close()

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    try:
        user_id = current_user.id
        if not user_id:
            logger.warning("No user ID found in session")
            flash('Utilisateur non trouvé dans la session.', 'danger')
            return redirect(url_for('login'))

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

            # Check if email is already used by another user
            cursor.execute("SELECT id FROM users WHERE email = %s AND id != %s", (new_email, user_id))
            if cursor.fetchone():
                logger.warning(f"User ID {user_id} attempted to use existing email {new_email}")
                flash('Cet email est déjà utilisé par un autre utilisateur.', 'danger')
                return redirect(url_for('profile'))

            # Handle password change if fields are filled
            if current_password or new_password or confirm_password:
                if not (current_password and new_password and confirm_password):
                    logger.warning(f"User ID {user_id} provided incomplete password fields")
                    flash('Tous les champs du mot de passe sont requis pour le changement.', 'danger')
                    return redirect(url_for('profile'))

                if new_password != confirm_password:
                    logger.warning(f"User ID {user_id} provided mismatched new passwords")
                    flash('Les nouveaux mots de passe ne correspondent pas.', 'danger')
                    return redirect(url_for('profile'))

                # Verify current password
                cursor.execute("SELECT password FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()
                if not user or not bcrypt.check_password_hash(user['password'], current_password):
                    logger.warning(f"User ID {user_id} provided incorrect current password")
                    flash('Mot de passe actuel incorrect.', 'danger')
                    return redirect(url_for('profile'))

                # Update password
                hashed_password = bcrypt.generate_password_hash(new_password).decode('utf-8')
                cursor.execute("UPDATE users SET password = %s WHERE id = %s", (hashed_password, user_id))
                logger.info(f"User ID {user_id} updated password")
                flash('Mot de passe mis à jour avec succès!', 'success')

            # Handle profile photo upload
            photo_path = None
            if profile_photo and allowed_profile_file(profile_photo.filename):
                if not os.path.exists(app.config['PROFILE_UPLOAD_FOLDER']):
                    os.makedirs(app.config['PROFILE_UPLOAD_FOLDER'])
                filename = secure_filename(profile_photo.filename)
                photo_path = os.path.join(app.config['PROFILE_UPLOAD_FOLDER'], f"{user_id}_{filename}")
                profile_photo.save(photo_path)
                photo_path = f"uploads/profiles/{user_id}_{filename}"

            # Update user info
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
            logger.info(f"User ID {user_id} updated profile: username={new_username}, email={new_email}")
            session['user'] = new_username
            flash('Profil mis à jour avec succès !', 'success')
            return redirect(url_for('profile'))

        # GET request - show profile
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
        if 'conn' in locals():
            cursor.close()
            conn.close()

@app.route('/logout')
@login_required
def logout():
    user_id = current_user.id
    session.pop('user_id', None)
    session.pop('user', None)
    logout_user()
    logger.info(f"User ID {user_id} logged out")
    flash('Vous avez été déconnecté.', 'success')
    return redirect(url_for('login'))

if __name__ == '__main__':
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    if not os.path.exists(app.config['PROFILE_UPLOAD_FOLDER']):
        os.makedirs(app.config['PROFILE_UPLOAD_FOLDER'])
    app.run(host='0.0.0.0', port=5000, debug=True)