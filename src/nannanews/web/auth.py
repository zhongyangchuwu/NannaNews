"""Authentication and administrator-only account management."""

import sqlite3

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from nannanews.runtime import connect_db
from .security import admin_required, login_required

def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (username,))
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            session.clear()
            session.permanent = True
            session['username'] = username
            return redirect(url_for('information_reader.index'))
        else:
            flash('Invalid username or password')

    return render_template('login.html')

@admin_required
def register_user():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = connect_db()
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                     (username, generate_password_hash(password)))
            conn.commit()
            flash('Registration successful')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already exists')
        finally:
            conn.close()

    return render_template('register.html')

def logout():
    session.clear()
    return redirect(url_for('login'))

@login_required
def change_password():
    if request.method == 'POST':
        original_password = request.form.get('original_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        if new_password != confirm_password:
            flash("New passwords do not match")
            return redirect(url_for('change_password'))
        conn = connect_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username=?", (session['username'],))
        user = c.fetchone()
        if user and check_password_hash(user[2], original_password):
            c.execute("UPDATE users SET password=? WHERE username=?", (generate_password_hash(new_password), session['username']))
            conn.commit()
            flash("Password successfully updated. Please log in again.")
            session.pop('username', None)
            conn.close()
            return redirect(url_for('login'))
        else:
            flash("Original password is incorrect")
            conn.close()
        return redirect(url_for('change_password'))
    return render_template('change_password.html')

@admin_required
def user_admin():
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT id, username FROM users")
    users = c.fetchall()
    conn.close()
    return render_template('user_admin.html', users=users)

@admin_required
def user_edit(user_id):
    conn = connect_db()
    c = conn.cursor()
    c.execute("SELECT id, username FROM users WHERE id=?", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        flash("User not found")
        return redirect(url_for('user_admin'))

    if request.method == 'POST':
        new_password = request.form.get('new_password')
        if not new_password:
            flash("请输入新密码")
            return redirect(url_for('user_edit', user_id=user_id))
        c.execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(new_password), user_id))
        conn.commit()
        conn.close()
        flash("密码更新成功")
        return redirect(url_for('user_admin'))
    conn.close()
    return render_template('user_edit.html', user=user)


def register(app):
    """Register controller routes without changing public endpoint names."""
    app.add_url_rule('/login', view_func=login, methods=['GET', 'POST'])
    app.add_url_rule('/register', endpoint='register', view_func=register_user, methods=['GET', 'POST'])
    app.add_url_rule('/logout', view_func=logout, methods=['POST'])
    app.add_url_rule('/change_password', view_func=change_password, methods=['GET', 'POST'])
    app.add_url_rule('/admin/user_admin', view_func=user_admin, methods=['GET'])
    app.add_url_rule('/admin/user_edit/<int:user_id>', view_func=user_edit, methods=['GET', 'POST'])
