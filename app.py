from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
import pdfplumber
from sqlalchemy import func
import os

# --- Konfigurasi Aplikasi ---
app = Flask(__name__)

# Konfigurasi Keamanan dan Database
app.config['SECRET_KEY'] = 'kunci_rahasia_super_aman_anda'
# Ganti KREDENSIAL ini dengan informasi MySQL Anda yang sebenarnya
app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:@localhost/rekap'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'

# Folder tempat menyimpan file PDF sementara
UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- Model Database ---

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False) # Menyimpan hash password

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)

class Transaction(db.Model):
    __tablename__ = 'transaction' 
    
    # KUNCI UTAMA DUPLIKASI: 5 KOLOM INI HARUS UNIK
    __table_args__ = (
        db.UniqueConstraint('unit', 'transaction_date', 'student_name', 'description', 'amount', name='_unique_transaction_fields'),
    )
    
    id = db.Column(db.Integer, primary_key=True)
    unit = db.Column(db.String(10), nullable=False) # SMP, SMA, MTS
    transaction_date = db.Column(db.DateTime, nullable=False)
    student_name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=False)
    method = db.Column(db.String(50), nullable=False) # Cash atau Saldo Ortu
    amount = db.Column(db.Float, nullable=False) # Nilai nominal

# --- MODEL BARU UNTUK MENCATAT SERAH TERIMA UANG DAN SALDO ---
class CashDisbursement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unit = db.Column(db.String(10), nullable=False) # Unit penerima
    amount = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    disbursement_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
# --- Logika Pemrosesan PDF (REVISI AKHIR: NORMALISASI WAKTU + 5 KUNCI) ---
def process_pdf_to_transactions(pdf_path, unit_name):
    transactions_to_add = [] 
    duplicate_count = 0      
    
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() 
            
            for table in tables:
                if table and len(table) > 1:
                    raw_rows = table[1:] 
                    
                    for row in raw_rows:
                        # Asumsi: [0:NO, 1:TANGGAL, 2:SISWA, 3:KETERANGAN, 4:METODE, 5:JUMLAH]
                        
                        jumlah_raw = row[5].strip() if len(row) > 5 and row[5] else None
                        
                        if not jumlah_raw or jumlah_raw == 'JUMLAH':
                            continue
                        
                        # Membersihkan nilai jumlah (Menghilangkan titik sebagai pemisah ribuan)
                        try:
                            amount = float(jumlah_raw.replace('.', '').replace(',', '.'))
                        except ValueError:
                            amount = 0.0

                        tanggal_str = row[1].strip() if row[1] else None
                        siswa_raw = row[2].strip() if row[2] else ""
                        
                        keterangan_mentah = (row[3].strip() if row[3] else "") + (row[4].strip() if row[4] else "")

                        method = 'Unknown'
                        if 'Cash' in keterangan_mentah:
                            method = 'Cash'
                        elif 'Saldo Ortu' in keterangan_mentah:
                            method = 'Saldo Ortu'
                        
                        description = keterangan_mentah.replace('Cash', '').replace('Saldo Ortu', '').replace('\n', ' ').strip()
                        student_name = siswa_raw.split('\n')[0].strip()

                        if tanggal_str and student_name and amount > 0 and method != 'Unknown':
                            try:
                                # Parsing tanggal dan waktu (presisi hingga menit)
                                raw_date = datetime.strptime(tanggal_str, '%d-%m-%Y %H:%M')
                                # **NORMALISASI WAKTU:** Nol-kan detik untuk konsistensi database
                                transaction_date = raw_date.replace(second=0) 
                            except ValueError:
                                # Jika gagal, gunakan waktu saat ini (detik dinolkan)
                                transaction_date = datetime.now().replace(second=0) 

                            # --- LOGIKA CEK DUPLIKASI MENGGUNAKAN 5 KUNCI YANG KETAT ---
                            
                            # Cek apakah sudah ada di antrian yang akan disimpan (dalam file yang sama)
                            is_duplicate_queue = any(
                                t.unit == unit_name and
                                t.student_name == student_name and
                                t.description == description and
                                t.amount == amount and
                                t.transaction_date == transaction_date
                                for t in transactions_to_add
                            )
                            
                            # Cek apakah transaksi sudah ada di database (sebelumnya)
                            is_duplicate_db = db.session.query(Transaction.id).filter(
                                Transaction.unit == unit_name,
                                Transaction.student_name == student_name,
                                Transaction.description == description,
                                Transaction.amount == amount,
                                Transaction.transaction_date == transaction_date
                            ).first()

                            if is_duplicate_db or is_duplicate_queue:
                                duplicate_count += 1
                                continue # Lewati transaksi ini

                            # Jika tidak duplikasi, tambahkan
                            transactions_to_add.append(Transaction(
                                unit=unit_name,
                                transaction_date=transaction_date,
                                student_name=student_name,
                                description=description,
                                method=method,
                                amount=amount
                            ))
                            
    return transactions_to_add, duplicate_count 

# --- Route Aplikasi ---

# Route Halaman Login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            flash('Login berhasil!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Username atau Password salah.', 'danger')
            
    return render_template('login.html', title='Login')

# Route Halaman Logout
@app.route('/logout')
@login_required 
def logout():
    logout_user()
    flash('Anda telah logout.', 'success')
    return redirect(url_for('login'))

# Route Dashboard (Halaman Utama)
@app.route('/')
@login_required
def dashboard():
    units = ['SMP', 'SMA', 'MTS'] 
    return render_template('dashboard.html', title='Dashboard', units=units)

# Route untuk Upload dan Proses PDF (Diperbarui untuk validasi duplikasi)
@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'pdf_file' not in request.files or 'unit_select' not in request.form:
        flash('Data formulir tidak lengkap.', 'danger')
        return redirect(url_for('dashboard'))

    file = request.files['pdf_file']
    unit_name = request.form.get('unit_select')
    
    if file.filename == '' or unit_name not in ['SMP', 'SMA', 'MTS']:
        flash('File tidak dipilih atau Unit tidak valid.', 'danger')
        return redirect(url_for('dashboard'))

    if file:
        filename = datetime.now().strftime('%Y%m%d%H%M%S') + '_' + file.filename
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)

        try:
            # Panggil fungsi pemrosesan PDF. Mengembalikan transaksi baru dan hitungan duplikasi.
            new_transactions, duplicated_count = process_pdf_to_transactions(file_path, unit_name)
            
            # Simpan hanya transaksi yang TIDAK GANDA ke database
            db.session.add_all(new_transactions)
            
            # Commit akan memicu IntegrityError jika ada duplikasi yang lolos
            db.session.commit()
            
            # Buat pesan sukses yang mencakup info duplikasi
            success_message = f'{len(new_transactions)} transaksi berhasil diekstrak dan disimpan untuk Unit {unit_name}.'
            if duplicated_count > 0:
                success_message += f' ({duplicated_count} duplikasi diabaikan saat pra-cek Python).'
            
            flash(success_message, 'success')

        except Exception as e:
            db.session.rollback()
            # Cek jika errornya adalah pelanggaran batasan unik (duplikasi)
            if 'IntegrityError' in str(e) or '1062' in str(e): # 1062 adalah kode MySQL IntegrityError
                 # Ini berarti ada duplikasi yang terdeteksi oleh database
                 flash(f'Gagal menyimpan: Ditemukan duplikasi data oleh database. Total {len(new_transactions)} transaksi dikembalikan. Pastikan 5 kunci unik (Unit, Waktu/Menit, Siswa, Keterangan, Jumlah) tidak ganda.', 'danger')
            else:
                 flash(f'Terjadi kesalahan saat memproses PDF: {str(e)}', 'danger')
        
        # Hapus file sementara setelah diproses
        os.remove(file_path)

    return redirect(url_for('unit_recap', unit=unit_name))

# --- ROUTE BARU UNTUK SERAH TERIMA UANG (DISBURSEMENT) ---
@app.route('/disburse/<unit>', methods=['POST'])
@login_required
def disburse_cash(unit):
    if unit not in ['SMP', 'SMA', 'MTS']:
        flash('Unit tidak valid.', 'danger')
        return redirect(url_for('dashboard'))

    try:
        # Menggunakan replace untuk membersihkan format Rupiah dari input
        amount_input = request.form.get('amount')
        # Menghilangkan titik dan mengubah koma menjadi titik (jika ada, meskipun input type=number)
        # Asumsi: input menggunakan pemisah ribuan titik
        amount = float(amount_input.replace('.', '').replace(',', '.')) 
        notes = request.form.get('notes')
    except ValueError:
        flash('Jumlah Serah Terima tidak valid.', 'danger')
        return redirect(url_for('unit_recap', unit=unit))

    if amount <= 0:
        flash('Jumlah serah terima harus lebih dari nol.', 'danger')
        return redirect(url_for('unit_recap', unit=unit))
        
    # --- HITUNG SALDO KAS TERSEDIA (TOTAL PEMASUKAN) ---
    # Total Cash (IN) = Semua transaksi Cash DAN Saldo Ortu (semua pemasukan)
    total_inflow = db.session.query(
        func.sum(Transaction.amount)
    ).filter(
        Transaction.unit == unit
    ).scalar() or 0.0

    # Cek Total Cash yang sudah diserahkan sebelumnya
    total_cash_out = db.session.query(
        func.sum(CashDisbursement.amount)
    ).filter(
        CashDisbursement.unit == unit
    ).scalar() or 0.0

    available_cash = total_inflow - total_cash_out
    
    # Cek ketersediaan saldo
    if amount > available_cash:
        # Memformat pesan error agar mudah dibaca
        formatted_amount = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        formatted_available = f"{available_cash:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")

        flash(f'Gagal: Jumlah serah terima (Rp {formatted_amount}) melebihi Saldo Kas Tersedia (Rp {formatted_available}).', 'danger')
        return redirect(url_for('unit_recap', unit=unit))

    # Catat serah terima
    disbursement = CashDisbursement(
        unit=unit,
        amount=amount,
        notes=notes,
        user_id=current_user.id
    )

    try:
        db.session.add(disbursement)
        db.session.commit()
        # Memformat pesan sukses
        formatted_amount = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        flash(f'Serah Terima Kas sebesar Rp {formatted_amount} untuk Unit {unit} berhasil dicatat.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Terjadi kesalahan database saat mencatat serah terima: {str(e)}', 'danger')

    return redirect(url_for('unit_recap', unit=unit))
    
# Route Rekap Per Unit (Updated to calculate Available Cash based on total inflow)
@app.route('/recap/<unit>')
@login_required
def unit_recap(unit):
    if unit not in ['SMP', 'SMA', 'MTS']:
        flash('Unit tidak ditemukan.', 'danger')
        return redirect(url_for('dashboard'))

    # --- 1. Ambil Keterangan Unik untuk Dropdown ---
    unique_descriptions_query = db.session.query(Transaction.description).filter(
        Transaction.unit == unit
    ).distinct().order_by(Transaction.description).all()

    unique_descriptions = [desc[0] for desc in unique_descriptions_query]
    
    # --- 2. Ambil Parameter Filter dari URL (Query String) ---
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    description_filter = request.args.get('description')
    method_filter = request.args.get('method')
    
    # Base Query: Filter selalu berdasarkan Unit yang dipilih
    base_query = Transaction.query.filter(Transaction.unit == unit)
    
    # --- 3. Aplikasikan Filter ke Query (Data Transaksi) ---
    
    # Filter Tanggal Mulai
    if start_date_str:
        try:
            start_dt = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            base_query = base_query.filter(Transaction.transaction_date >= start_dt)
        except ValueError:
            flash('Format Tanggal Mulai tidak valid.', 'warning')

    # Filter Tanggal Sampai
    if end_date_str:
        try:
            end_dt = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            # Tambahkan 1 hari untuk mencakup seluruh hari end_dt
            end_dt_inclusive = end_dt + timedelta(days=1)
            base_query = base_query.filter(Transaction.transaction_date < end_dt_inclusive)
        except ValueError:
            flash('Format Tanggal Sampai tidak valid.', 'warning')
            
    # Filter Keterangan (Pencocokan Eksak)
    if description_filter:
        base_query = base_query.filter(Transaction.description == description_filter)

    # Filter Metode Pembayaran (Pencocokan Eksak)
    if method_filter in ['Cash', 'Saldo Ortu']:
        base_query = base_query.filter(Transaction.method == method_filter)

    # --- 4. Hitung Total Cash dan Total Saldo Ortu ---
    
    # Kueri untuk mendapatkan total CASH dan SALDO ORTU dari TRANSAKSI yang sudah difilter
    recap_data = base_query.with_entities(
        Transaction.method,
        func.sum(Transaction.amount).label('total')
    ).group_by(
        Transaction.method
    ).all()

    # total_cash adalah cash masuk (method='Cash')
    total_cash = next((r.total for r in recap_data if r.method == 'Cash'), 0.0)
    # total_saldo adalah saldo ortu masuk (method='Saldo Ortu')
    total_saldo = next((r.total for r in recap_data if r.method == 'Saldo Ortu'), 0.0)
    
    # --- 4.1. Hitung Saldo Kas Tersedia (Available Cash) ---
    
    # Total Pemasukan adalah jumlah dari Cash dan Saldo Ortu
    total_inflow = total_cash + total_saldo
    
    # Ambil total serah terima yang sudah dilakukan untuk Unit ini (TIDAK DIPENGARUHI FILTER TANGGAL/KETERANGAN)
    total_disbursed = db.session.query(
        func.sum(CashDisbursement.amount)
    ).filter(
        CashDisbursement.unit == unit
    ).scalar() or 0.0
    
    # Saldo Kas Tersedia = Total Pemasukan - Total Cash Keluar (Serah Terima)
    available_cash = total_inflow - total_disbursed
    
    # Ambil 10 serah terima kas terakhir untuk ditampilkan
    disbursements = CashDisbursement.query.filter_by(unit=unit).order_by(CashDisbursement.disbursement_date.desc()).limit(10).all()

    # --- 5. Ambil Detail Transaksi (Tabel) ---
    
    # Ambil 100 transaksi terbaru dari query yang sudah difilter
    transactions = base_query.order_by(Transaction.transaction_date.desc()).limit(100).all()

    # --- 6. Kirim data ke Template ---
    return render_template(
        'unit_recap.html', 
        title=f'Rekap Pembayaran Unit {unit}',
        unit=unit,
        total_cash=total_cash,       # Total Cash (IN) saja
        total_saldo=total_saldo,     # Total Saldo Ortu (IN) saja
        available_cash=available_cash, # Total Inflow - Disbursement
        disbursements=disbursements,   # Riwayat Serah Terima
        transactions=transactions,
        # Kirim kembali nilai filter agar form tetap terisi
        start_date=start_date_str,
        end_date=end_date_str,
        description=description_filter,
        method=method_filter,
        unique_descriptions=unique_descriptions
    )

if __name__ == '__main__':
    # Pastikan tabel database dibuat sebelum aplikasi berjalan
    with app.app_context():
        # *** PERHATIAN: JIKA INI PERTAMA KALI ANDA MENJALANKAN DENGAN CODE INI ***
        # AGAR CONSTRAINT UNIK BARU AKTIF, ANDA MUNGKIN PERLU MENGHAPUS DATABASE 
        # LAMA (REKAP) DAN IMPORT ULANG rekap (3).sql, ATAU HANYA MENGHAPUS TABEL 'transaction'.
        db.create_all()
        
        # Contoh: Buat admin pertama jika belum ada (hanya untuk testing)
        if User.query.filter_by(username='admin').first() is None:
            admin = User(username='admin')
            admin.set_password('admin123') # Ganti password ini
            db.session.add(admin)
            db.session.commit()
            print("Akun admin dibuat: Username: admin, Password: admin123")
            
    app.run(debug=False) # Dalam hosting, 'debug=True' harus dihapus