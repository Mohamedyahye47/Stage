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
from typing import Dict, Optional, Union

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
        return "{:,.2f}".format(float(value)).replace(",", " ").replace(".", ",")
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

    try:
        # Get recent invoices
        cursor.execute("""
            SELECT 
                ot_number,
                DATE_FORMAT(invoice_date, '%Y-%m-%d') as invoice_date,
                societe,
                produit,
                quantite,
                total_usd,
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
                SUM(total_usd) as total_value,
                AVG(total_usd) as avg_value
            FROM invoices
        """)
        stats = cursor.fetchone()

        # Calculate top société by USD value
        cursor.execute("""
            SELECT 
                societe,
                SUM(total_usd) as total_usd,
                (SUM(total_usd) / (SELECT SUM(total_usd) FROM invoices) * 100) as percentage
            FROM invoices
            GROUP BY societe
            ORDER BY total_usd DESC
            LIMIT 1
        """)
        top_societe = cursor.fetchone()
        stats['top_societe_name'] = top_societe['societe'] if top_societe else 'N/A'
        stats['top_societe_percent'] = round(top_societe['percentage'], 2) if top_societe else 0

        # Monthly totals for chart
        cursor.execute("""
            SELECT 
                DATE_FORMAT(invoice_date, '%Y-%m') as month,
                SUM(total_usd) as total
            FROM invoices
            GROUP BY month
            ORDER BY month
        """)
        monthly_data = cursor.fetchall()

        # Product distribution (by quantity)
        cursor.execute("""
            SELECT 
                produit,
                SUM(quantite) as total_quantite,
                SUM(total_usd) as total_usd
            FROM invoices
            GROUP BY produit
        """)
        product_data_raw = cursor.fetchall()

        # Calculate percentages by quantity
        total_quantite = sum(row['total_quantite'] for row in product_data_raw) or 1
        product_data = [{
            'produit': row['produit'],
            'total_quantite': row['total_quantite'],
            'total_usd': row['total_usd'],
            'percentage': round((row['total_quantite'] / total_quantite) * 100, 2)
        } for row in product_data_raw]

        # Company/Destination data
        cursor.execute("""
            SELECT societe, destination, quantite, total_usd 
            FROM invoices
        """)
        graph_data = cursor.fetchall()

        # Process with Pandas
        df = pd.DataFrame(graph_data)

        # Process societe data
        societe_data = df.groupby('societe').agg({
            'quantite': 'sum',
            'total_usd': 'sum'
        }).reset_index()

        total_quantite = societe_data['quantite'].sum() or 1
        total_usd = societe_data['total_usd'].sum() or 1

        societe_data['percentage_quantite'] = (societe_data['quantite'] / total_quantite * 100).round(2)
        societe_data['percentage_usd'] = (societe_data['total_usd'] / total_usd * 100).round(2)

        societe_labels = societe_data['societe'].tolist()
        societe_pourcentages = societe_data['percentage_quantite'].tolist()
        societe_usd_pourcentages = societe_data['percentage_usd'].tolist()

        # Process destination data
        datasets = []
        if not df.empty:
            destinations = df['destination'].unique().tolist()
            colors = ['#4E79A7', '#F28E2B', '#E15759', '#76B7B2', '#59A14F']

            for i, dest in enumerate(destinations):
                dest_data = df[df['destination'] == dest]
                dest_percentages = (dest_data.groupby('societe')['quantite'].sum() / total_quantite * 100).round(2)
                datasets.append({
                    'label': dest,
                    'data': [dest_percentages.get(societe, 0) for societe in societe_labels],
                    'backgroundColor': colors[i % len(colors)]
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
        'societe_usd_pourcentages': societe_usd_pourcentages or [],
        'datasets': datasets or []
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
            societe_usd_pourcentages=[],
            datasets=[]
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
    app.run(debug=True)