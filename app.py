
import json
from decimal import Decimal, InvalidOperation
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import mysql.connector


# ---------- DATABASE CONNECTION ----------

def connect_db():
    return mysql.connector.connect(
        host="localhost",
        user="root",
        password="Lohith@1918",
        database="smart_inventory",
        port=3306
    )


# ---------- JSON HELPERS ----------

def json_safe(value):
    """Convert database values into JSON-compatible values."""
    if isinstance(value, Decimal):
        return float(value)
    return value


class InventoryHandler(SimpleHTTPRequestHandler):

    def send_json(self, status_code, data):
        body = json.dumps(data, default=json_safe).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 100_000:
                raise ValueError("Invalid request size.")

            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))

            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object.")

            return data

        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Please send valid JSON data.")

    def get_products(self, search="", threshold=None):
        conn = connect_db()
        cursor = conn.cursor(dictionary=True)

        try:
            sql = """
                SELECT product_id, product_name, category, price, quantity
                FROM products
            """
            conditions = []
            params = []

            if search:
                conditions.append(
                    "(product_name LIKE %s OR category LIKE %s)"
                )
                pattern = f"%{search}%"
                params.extend([pattern, pattern])

            if threshold is not None:
                conditions.append("quantity <= %s")
                params.append(threshold)

            if conditions:
                sql += " WHERE " + " AND ".join(conditions)

            sql += " ORDER BY product_id DESC"
            cursor.execute(sql, tuple(params))
            products = cursor.fetchall()

            return products

        finally:
            cursor.close()
            conn.close()

    # ---------- GET REQUESTS ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        try:
            if parsed.path == "/api/products":
                search = params.get("q", [""])[0].strip()
                products = self.get_products(search=search)

                self.send_json(200, {
                    "products": products,
                    "total": len(products)
                })
                return

            if parsed.path == "/api/low-stock":
                threshold_text = params.get("threshold", ["10"])[0]

                try:
                    threshold = int(threshold_text)
                    if threshold < 0:
                        raise ValueError
                except ValueError:
                    self.send_json(400, {
                        "error": "Threshold must be a non-negative integer."
                    })
                    return

                products = self.get_products(threshold=threshold)

                self.send_json(200, {
                    "products": products,
                    "total": len(products)
                })
                return

            # Serve index.html, style.css and other local files.
            if parsed.path.startswith("/api/"):
                self.send_json(404, {"error": "API endpoint not found."})
                return

            super().do_GET()

        except mysql.connector.Error:
            self.send_json(500, {
                "error": "Database error. Check your MySQL connection."
            })
        except Exception:
            self.send_json(500, {
                "error": "An unexpected server error occurred."
            })

    # ---------- POST REQUESTS ----------

    def do_POST(self):
        parsed = urlparse(self.path)

        try:
            data = self.read_json()

            if parsed.path == "/api/products":
                name = data.get("product_name")
                category = data.get("category")

                if not isinstance(name, str) or not name.strip():
                    self.send_json(400, {
                        "error": "Product name is required."
                    })
                    return

                if not isinstance(category, str) or not category.strip():
                    self.send_json(400, {
                        "error": "Category is required."
                    })
                    return

                name = name.strip()
                category = category.strip()

                if len(name) > 100 or len(category) > 50:
                    self.send_json(400, {
                        "error": "Product name or category is too long."
                    })
                    return

                try:
                    price = Decimal(str(data.get("price", "")))
                    quantity = int(str(data.get("quantity", "")))

                    if (
                        not price.is_finite()
                        or price <= 0
                        or price.as_tuple().exponent < -2
                    ):
                        raise ValueError

                    if quantity < 0 or quantity > 2147483647:
                        raise ValueError

                except (InvalidOperation, ValueError, TypeError):
                    self.send_json(400, {
                        "error": "Enter a valid positive price and a non-negative whole-number quantity."
                    })
                    return

                conn = connect_db()
                cursor = conn.cursor()

                try:
                    cursor.execute("""
                        INSERT INTO products
                            (product_name, category, price, quantity)
                        VALUES (%s, %s, %s, %s)
                    """, (name, category, price, quantity))

                    product_id = cursor.lastrowid
                    conn.commit()

                    self.send_json(201, {
                        "message": "Product added successfully.",
                        "product_id": product_id
                    })

                except Exception:
                    conn.rollback()
                    raise

                finally:
                    cursor.close()
                    conn.close()

                return

            if parsed.path == "/api/sales":
                try:
                    product_id = int(str(data.get("product_id", "")))
                    quantity_sold = int(str(data.get("quantity_sold", "")))

                    if product_id <= 0 or quantity_sold <= 0:
                        raise ValueError

                except (ValueError, TypeError):
                    self.send_json(400, {
                        "error": "Enter a valid product ID and quantity."
                    })
                    return

                conn = connect_db()
                cursor = conn.cursor(dictionary=True)

                try:
                    # Lock the product row while checking available stock.
                    cursor.execute("""
                        SELECT product_id, product_name, price, quantity
                        FROM products
                        WHERE product_id = %s
                        FOR UPDATE
                    """, (product_id,))

                    product = cursor.fetchone()

                    if product is None:
                        conn.rollback()
                        self.send_json(404, {
                            "error": "Product not found."
                        })
                        return

                    if product["quantity"] < quantity_sold:
                        conn.rollback()
                        self.send_json(400, {
                            "error": (
                                f"Insufficient stock. Available quantity: "
                                f"{product['quantity']}."
                            )
                        })
                        return

                    unit_price = Decimal(str(product["price"]))
                    total_amount = unit_price * quantity_sold

                    cursor.execute("""
                        INSERT INTO sales
                            (product_id, quantity_sold, unit_price, total_amount)
                        VALUES (%s, %s, %s, %s)
                    """, (
                        product_id,
                        quantity_sold,
                        unit_price,
                        total_amount
                    ))

                    # Your existing after_sale_insert trigger reduces stock.
                    conn.commit()

                    self.send_json(201, {
                        "message": "Sale recorded successfully.",
                        "total_amount": str(total_amount)
                    })

                except Exception:
                    conn.rollback()
                    raise

                finally:
                    cursor.close()
                    conn.close()

                return

            self.send_json(404, {"error": "API endpoint not found."})

        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except mysql.connector.Error:
            self.send_json(500, {
                "error": "Database error. Check your MySQL tables and connection."
            })
        except Exception:
            self.send_json(500, {
                "error": "An unexpected server error occurred."
            })

    # ---------- UPDATE STOCK ----------

    def do_PUT(self):
        parsed = urlparse(self.path)

        if not parsed.path.startswith("/api/products/"):
            self.send_json(404, {"error": "API endpoint not found."})
            return

        try:
            product_id = int(parsed.path.rsplit("/", 1)[-1])
            data = self.read_json()
            quantity = int(str(data.get("quantity", "")))

            if product_id <= 0 or quantity < 0 or quantity > 2147483647:
                raise ValueError

            conn = connect_db()
            cursor = conn.cursor()

            try:
                cursor.execute("""
                    UPDATE products
                    SET quantity = %s
                    WHERE product_id = %s
                """, (quantity, product_id))

                if cursor.rowcount == 0:
                    cursor.execute(
                        "SELECT product_id FROM products WHERE product_id = %s",
                        (product_id,)
                    )

                    if cursor.fetchone() is None:
                        conn.rollback()
                        self.send_json(404, {
                            "error": "Product not found."
                        })
                        return

                conn.commit()
                self.send_json(200, {
                    "message": "Stock updated successfully."
                })

            except Exception:
                conn.rollback()
                raise

            finally:
                cursor.close()
                conn.close()

        except (ValueError, TypeError):
            self.send_json(400, {
                "error": "Use a valid product ID and non-negative whole-number quantity."
            })
        except mysql.connector.Error:
            self.send_json(500, {
                "error": "Database error while updating stock."
            })
        except Exception:
            self.send_json(500, {
                "error": "An unexpected server error occurred."
            })


# ---------- START SERVER ----------

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8000), InventoryHandler)
    print("Smart Inventory Management System is running.")
    print("Open http://127.0.0.1:8000 in your browser.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
