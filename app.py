from flask import Flask, render_template, request, redirect, url_for, flash, send_file
import os
import PyPDF2
import re
from datetime import datetime
import mysql.connector
from werkzeug.utils import secure_filename
import openpyxl
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'your_secret_key'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['ALLOWED_EXTENSIONS'] = {'pdf'}

# Configuration de la base de données
db_config = {
    'host': 'localhost',
    'user': 'root',
    'password': 'MedYahya47!!',
    'database': 'data_upload'
}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_db_connection():
    return mysql.connector.connect(**db_config)

def extract_invoice_data(pdf_path):
    text = ""
    with open(pdf_path, 'rb') as file:
        pdf_reader = PyPDF2.PdfReader(file)
        for page in pdf_reader.pages:
            text += page.extract_text() + "\n"

    # Extraction améliorée du nom de l'entreprise
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

    # Extraction améliorée du produit
    product_match = re.search(r'PRODUIT\s*\|\s*([^\|]+)', text)
    if not product_match:
        product_match = re.search(r'Produit:\s*([^\n]+)', text)
    produit = product_match.group(1).strip() if product_match else None

    # Extraction améliorée de la quantité
    quantite_match = re.search(r'QUANTITE\s*\|\s*([\d\.,]+)', text)
    if not quantite_match:
        quantite_match = re.search(r'Quantité \(Tonnes Métriques\)\s*([\d\.,]+)', text)
    quantite = float(quantite_match.group(1).replace(',', '').replace("'", "")) if quantite_match else None

    # Calcul amélioré du total sans fret
    total_usd_match = re.search(r'Montant total de la facture\s*\$([\d\',]+\.\d{2})', text)
    total_usd = float(total_usd_match.group(1).replace("'", "").replace(",", "")) if total_usd_match else None

    fret_match = re.search(r'FRET USD / Tonne Métrique\s*\$([\d\.,]+)', text)
    fret = float(fret_match.group(1).replace(",", "")) if fret_match else None

    # Calcul du total sans fret si tous les composants sont présents
    total_sans_fret = round(total_usd - (fret * quantite), 2) if total_usd and fret and quantite else None

    data = {
        'ot_number': re.search(r'Ordre de transfert No:\s*(\d+)', text).group(1) if re.search(
            r'Ordre de transfert No:\s*(\d+)', text) else None,
        'invoice_date': re.search(r'Genève, le (\d{2}\.\d{2}\.\d{4})', text).group(1) if re.search(
            r'Genève, le (\d{2}\.\d{2}\.\d{4})', text) else None,
        'destination': re.search(r'Terminal:\s*([^\n]+)', text).group(1).split()[0] if re.search(
            r'Terminal:\s*([^\n]+)', text) else None,
        'societe': societe,
        'produit': produit,
        'quantite': quantite,
        'prix_unitaire': float(
            re.search(r'Prix Unitaire\s+\$([\d\',]+\.\d{2})', text).group(1).replace("'", "").replace(",", "")) if re.search(
            r'Prix Unitaire\s+\$([\d\',]+\.\d{2})', text) else None,
        'total_usd': total_usd,
        'fret': fret,
        'total_sans_fret': total_sans_fret
    }

    return data

def get_dashboard_data():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Obtenir les factures récentes
    cursor.execute("""
        SELECT 
            ot_number,
            DATE_FORMAT(invoice_date, '%Y-%m-%d') as invoice_date,
            societe,
            produit,
            FORMAT(quantite, 3) as quantite,
            FORMAT(total_usd, 2) as total_usd,
            FORMAT(total_sans_fret, 2) as total_sans_fret
        FROM invoices 
        ORDER BY invoice_date DESC 
        LIMIT 10
    """)
    invoices = cursor.fetchall()

    # Obtenir les statistiques sommaires
    cursor.execute("""
        SELECT 
            COUNT(*) as total_invoices,
            SUM(total_usd) as total_value,
            AVG(total_usd) as avg_value
        FROM invoices
    """)
    stats = cursor.fetchone()

    # Total mensuel pour le graphique
    cursor.execute("""
        SELECT 
            DATE_FORMAT(invoice_date, '%Y-%m') as month,
            SUM(total_usd) as total
        FROM invoices
        GROUP BY month
        ORDER BY month
    """)
    monthly_data = cursor.fetchall() or []

    # Répartition des produits pour le graphique
    cursor.execute("""
        SELECT 
            produit,
            IFNULL(SUM(total_usd), 0) as total
        FROM invoices
        GROUP BY produit
    """)
    product_data = cursor.fetchall() or []

    cursor.close()
    conn.close()

    return invoices, stats, monthly_data, product_data

@app.route('/')
def dashboard():
    invoices, stats, monthly_data, product_data = get_dashboard_data()
    return render_template('dashboard.html', invoices=invoices, stats=stats, monthly_data=monthly_data, product_data=product_data)

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

                if invoice_data['invoice_date']:
                    invoice_date = datetime.strptime(invoice_data['invoice_date'], '%d.%m.%Y').date()
                else:
                    invoice_date = None

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

            except Exception as e:
                flash(f'Erreur lors du traitement de la facture : {str(e)}', 'danger')
                return redirect(request.url)

    return render_template('upload.html')

@app.route('/download_xlsx')
def download_xlsx():
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

    # Créer un fichier XLSX
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Factures"

    headers = ['Numéro OT', 'Date de Facture', 'Destination', 'Société', 'Produit', 'Quantité', 'Prix Unitaire', 'Total USD', 'Fret', 'Total sans Fret']
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
    app.run(debug=True)