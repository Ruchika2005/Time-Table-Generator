from flask import Flask, render_template, request, redirect, session, url_for
import mysql.connector
from config import DB_CONFIG
from collections import defaultdict
import random
import time
import heapq


app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # 🔐 change this for production

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)



@app.route('/generate_timetable', methods=['POST'])
def generate_timetable():
    if not session.get('admin_logged_in'):
        return redirect(url_for('login'))


    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)


    # Clear old timetable
    cursor.execute("DELETE FROM timetable")


    # Load settings
    cursor.execute("SELECT * FROM settings LIMIT 1")
    settings = cursor.fetchone()
    if not settings:
        return "Settings not configured."


    working_days = settings['working_days']  # should be 6
    lectures_per_day = settings['lectures_per_day']  # should be 6
    total_slots = working_days * lectures_per_day  # 36


    # Get all classes
    cursor.execute("SELECT id FROM classes")
    all_classes = [row['id'] for row in cursor.fetchall()]


    # Get subject-teacher assignments
    cursor.execute("""
        SELECT cs.class_id, cs.subject_id, cs.weekly_lectures_required, t.id AS teacher_id
        FROM class_subjects cs
        JOIN teachers t ON cs.subject_id = t.subject_id
    """)
    assignments = cursor.fetchall()


    # Group by class
    class_assignments = defaultdict(list)
    for a in assignments:
        class_assignments[a['class_id']].extend([{
            "subject_id": a['subject_id'],
            "teacher_id": a['teacher_id']
        }] * a['weekly_lectures_required'])


    # Final timetable per class: timetable[class_id][day][period] = (subject_id, teacher_id)
    timetable = defaultdict(lambda: [[None for _ in range(lectures_per_day)] for _ in range(working_days)])


    # Teacher occupied slots: teacher_occupied[teacher_id][day][period]
    teacher_occupied = defaultdict(lambda: [[False for _ in range(lectures_per_day)] for _ in range(working_days)])


    def backtrack(class_id, slots, index):
        if index == len(slots):
            return True  # All lectures assigned


        subject_id = slots[index]["subject_id"]
        teacher_id = slots[index]["teacher_id"]


        # Shuffle day and period to avoid bias
        day_periods = [(d, p) for d in range(working_days) for p in range(lectures_per_day)]
        random.shuffle(day_periods)


        for day, period in day_periods:
            if timetable[class_id][day][period] is not None:
                continue
            if teacher_occupied[teacher_id][day][period]:
                continue
            if any(timetable[class_id][day][p] and timetable[class_id][day][p][0] == subject_id for p in range(lectures_per_day)):
                continue  # subject already taught on this day


            # Assign
            timetable[class_id][day][period] = (subject_id, teacher_id)
            teacher_occupied[teacher_id][day][period] = True


            if backtrack(class_id, slots, index + 1):
                return True


            # Backtrack
            timetable[class_id][day][period] = None
            teacher_occupied[teacher_id][day][period] = False


        return False


    for class_id in all_classes:
        print(f"📚 Generating for Class {class_id}")
        lecture_slots = class_assignments[class_id]
        if len(lecture_slots) != total_slots:
            return f"❌ Error: Class {class_id} needs exactly {total_slots} lectures, but got {len(lecture_slots)}"


        random.shuffle(lecture_slots)  # Randomize for diversity
        success = backtrack(class_id, lecture_slots, 0)


        if not success:
            return f"❌ Timetable could not be generated for Class {class_id} due to conflicts."


    # Insert into DB
    for class_id, days in timetable.items():
        for day in range(working_days):
            for period in range(lectures_per_day):
                subject_teacher = timetable[class_id][day][period]
                if subject_teacher:
                    subject_id, teacher_id = subject_teacher
                    cursor.execute("""
                        INSERT INTO timetable (class_id, day, period_no, subject_id, teacher_id)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (class_id, f"Day {day + 1}", period + 1, subject_id, teacher_id))


    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('admin_dashboard'))


# Admin Login Page
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM admin WHERE username = %s", (username,))
        admin = cursor.fetchone()
        cursor.close()
        conn.close()

        if admin and admin['password'] == password:
            session['admin_logged_in'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = "Invalid username or password"

    return render_template('login.html', error=error)

# Admin Dashboard
@app.route('/dashboard')
def admin_dashboard():
    if not session.get('admin_logged_in'):
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM classes")
    classes = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('admin_dashboard.html', admin=session.get('admin_username'), classes=classes)

# Admin Logout
@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('login'))

# Protected Main Page
@app.route('/')
def index():
    if not session.get('admin_logged_in'):
        return redirect(url_for('login'))

    return render_template('index.html')

@app.route('/timetable')
def view_timetable():
    if not session.get('admin_logged_in'):
        return redirect(url_for('login'))

    class_id = request.args.get('class_id')
    if not class_id:
        return "Missing class_id in request"

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get class info
    cursor.execute("SELECT * FROM classes WHERE id = %s", (class_id,))
    class_info = cursor.fetchone()
    if not class_info:
        return f"No class found with id {class_id}"

    # Get timetable data
    cursor.execute("""
        SELECT tt.day, tt.period_no, s.name AS subject, t.name AS teacher
        FROM timetable tt
        JOIN subjects s ON tt.subject_id = s.id
        JOIN teachers t ON tt.teacher_id = t.id
        WHERE tt.class_id = %s
        ORDER BY tt.day, tt.period_no
    """, (class_id,))
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    # Prepare data for matrix format
    timetable = defaultdict(dict)
    days_set = set()
    max_period = 0

    for row in rows:
        day = row['day']
        period = row['period_no']
        timetable[day][period] = {
            'subject': row['subject'],
            'teacher': row['teacher']
        }
        days_set.add(day)
        max_period = max(max_period, period)

    # Sort days in natural order like Day 1, Day 2, ...
    sorted_days = sorted(days_set, key=lambda d: int(d.split()[-1]))

    return render_template('timetable.html',
                           class_name=class_info['name'],
                           timetable=timetable,
                           days=sorted_days,
                           lectures_per_day=max_period)
if __name__ == '__main__':
    app.run(debug=True)
