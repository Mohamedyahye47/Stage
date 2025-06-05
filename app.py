from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import os
import PyPDF2
import re
from datetime import datetime
import mysql.connector
from werkzeug.utils import secure_filename
import openpyxl
from io import BytesIO
import pandas as pd
import numpy as np
from scipy.stats import chi2_contingency

app = Flask(__name__)
app.secret_key = 'yahya'
app.config['UPLOAD_FOLDER'] = 'Uploads'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}

# Database configuration
db_config = {
    'host': 'localhost',
    'user': 'root',
    'password': 'MedYahya47!!',
    'database': 'data_upload'
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

def get_db_connection():
    try:
        return mysql.connector.connect(**db_config)
    except mysql.connector.Error as err:
        flash(f'Erreur de connexion à la base de données : {str(err)}', 'danger')
        raise

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
            r'FACTURE\s+COMMERCIALE\s+No\s+[A-Z]?(\d{4,})'
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
        print(f"Error parsing date: {e}")

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

def get_dashboard_data():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Get recent invoices
        cursor.execute("""
            SELECT 
                ot_number,
                DATE_FORMAT(invoice_date, '%Y-%m-%d') as invoice_date,
                societe,
                produit,
                COALESCE(quantite, 0) as quantite,
                COALESCE(total_usd, 0) as total_usd,
                total_sans_fret
            FROM invoices 
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        invoices = cursor.fetchall()

        # Get summary statistics
        cursor.execute("""
            SELECT 
                COUNT(*) as total_invoices,
                COALESCE(SUM(total_usd), 0) as total_value,
                COALESCE(AVG(total_usd), 0) as avg_value
            FROM invoices
        """)
        stats = cursor.fetchone()

        # Calculate top société by USD value
        cursor.execute("""
            SELECT 
                societe,
                COALESCE(SUM(total_usd), 0) as total_usd,
                (COALESCE(SUM(total_usd), 0) / NULLIF((SELECT SUM(total_usd) FROM invoices), 0) * 100) as percentage
            FROM invoices
            GROUP BY societe
            ORDER BY total_usd DESC
            LIMIT 1
        """)
        top_societe = cursor.fetchone()
        stats['top_societe_name'] = top_societe['societe'] if top_societe else 'N/A'
        stats['top_societe_percent'] = round(top_societe['percentage'], 1) if top_societe else 0

        # Monthly totals for chart (remains in USD)
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
        cursor.execute("""
            SELECT 
                DATE_FORMAT(invoice_date, '%Y-%m') as month,
                societe,
                COALESCE(SUM(total_usd), 0) as total
            FROM invoices
            GROUP BY month, societe
        """)
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

        # Product distribution (already in percentages by quantity)
        cursor.execute("""
            SELECT 
                COALESCE(produit, 'Inconnu') as produit,
                COALESCE(SUM(quantite), 0) as total_quantite,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            GROUP BY produit
        """)
        product_data_raw = cursor.fetchall()
        total_quantite = sum(row['total_quantite'] for row in product_data_raw) or 1
        product_data = [{
            'produit': row['produit'],
            'total_quantite': row['total_quantite'],
            'total_usd': row['total_usd'],
            'percentage': round((row['total_quantite'] / total_quantite) * 100, 1)
        } for row in product_data_raw]

        # Company data (already in percentages by quantity)
        cursor.execute("""
            SELECT 
                COALESCE(societe, 'Inconnu') as societe,
                COALESCE(SUM(quantite), 0) as total_quantite,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            GROUP BY societe
        """)
        societe_data_raw = cursor.fetchall()
        total_quantite_all = sum(row['total_quantite'] for row in societe_data_raw) or 1
        societe_labels = [row['societe'] for row in societe_data_raw]
        societe_pourcentages = [
            round((row['total_quantite'] / total_quantite_all) * 100, 1) for row in societe_data_raw
        ]

        # Société/Destination data (convert to percentages per société)
        cursor.execute("""
            SELECT 
                COALESCE(societe, 'Inconnu') as societe,
                COALESCE(destination, 'Inconnu') as destination,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            GROUP BY societe, destination
        """)
        destination_data = cursor.fetchall()
        societe_destination_datasets = []
        cramers_v_societe_destination = None

        if destination_data:
            # Group by société and calculate percentages
            societe_totals = {}
            for row in destination_data:
                societe = row['societe']
                societe_totals[societe] = societe_totals.get(societe, 0) + row['total_usd']

            # Prepare datasets for each destination
            destinations = sorted(set(row['destination'] for row in destination_data))
            colors = ['#4e73df', '#1cc88a', '#36b9cc', '#f6c23e', '#e74a3b']  # Chart.js colors

            for i, dest in enumerate(destinations):
                dest_data = [row for row in destination_data if row['destination'] == dest]
                percentages = []
                for societe in societe_labels:
                    matching = next((row for row in dest_data if row['societe'] == societe), None)
                    total = societe_totals.get(societe, 1)  # Avoid division by zero
                    percentage = (matching['total_usd'] / total * 100) if matching else 0
                    percentages.append(round(percentage, 1))

                societe_destination_datasets.append({
                    'label': dest,
                    'data': percentages,
                    'backgroundColor': colors[i % len(colors)],
                    'borderColor': colors[i % len(colors)],
                    'borderWidth': 1
                })

            # Calculate Cramér's V for société vs destination
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

        # Product vs Société data (convert to percentages per société)
        cursor.execute("""
            SELECT 
                COALESCE(societe, 'Inconnu') as societe,
                COALESCE(produit, 'Inconnu') as produit,
                COALESCE(SUM(total_usd), 0) as total_usd
            FROM invoices
            GROUP BY societe, produit
        """)
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
                    total = societe_totals.get(societe, 1)  # Avoid division by zero
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
        'cramers_v': cramers_v,
        'cramers_v_monthly': cramers_v_monthly,
        'cramers_v_societe_destination': cramers_v_societe_destination
    }

@app.route('/')
def dashboard():
    try:
        data = get_dashboard_data()
        return render_template('dashboard.html', **data)
    except Exception as e:
        print(f"Erreur dans la route du tableau de bord : {str(e)}")
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
            cramers_v_societe_destination=None
        )

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('Aucun fichier sélectionné', 'danger')
            return redirect(request.url)

        file = request.files['file']
        societe = request.form.get('societe')

        if file.filename == '':
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

                conn = get_db_connection()
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
                cursor.close()
                conn.close()

                flash('Facture traitée avec succès !', 'success')
                return redirect(url_for('dashboard'))

            except mysql.connector.Error as err:
                if err.errno == 1062:
                    flash(f"Erreur : L'ordre de transfert n° {invoice_data['ot_number']} existe déjà", 'danger')
                else:
                    flash(f'Erreur de base de données : {str(err)}', 'danger')
                return redirect(request.url)
            except Exception as e:
                flash(f'Erreur lors du traitement de la facture : {str(e)}', 'danger')
                return redirect(request.url)

    return render_template('upload.html')

@app.route('/telecharger_excel')
def telecharger_excel():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
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
    """)
    invoices = cursor.fetchall()
    cursor.close()
    conn.close()

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
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='factures.xlsx'
    )

if __name__ == '__main__':
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    app.run(host='0.0.0.0', port=5000, debug=True)