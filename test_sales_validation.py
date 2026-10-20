import unittest
import app as shop
from unittest.mock import patch, MagicMock

class SalesValidationTest(unittest.TestCase):

    def setUp(self):
        shop.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False
        )
        self.client = shop.app.test_client()

        # Simulate a logged-in cashier without using the database.
        with self.client.session_transaction() as session:
            session["user_id"] = 2
            session["username"] = "cashier1"
            session["role"] = "cashier"

    def check_invalid_sale(self, sale_data):
        with patch.object(shop, "db_cursor") as mock_db:
            response = self.client.post(
                "/sales/add",
                data=sale_data
            )

            self.assertEqual(response.status_code, 302)
            mock_db.assert_not_called()

    def test_zero_quantity_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "1",
            "product_id": "1",
            "quantity": "0",
            "discount_pct": "0",
            "payment_mode": "Cash"
        })

    def test_invalid_discount_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "1",
            "product_id": "1",
            "quantity": "1",
            "discount_pct": "101",
            "payment_mode": "Cash"
        })

    def test_invalid_payment_mode_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "1",
            "product_id": "1",
            "quantity": "1",
            "discount_pct": "0",
            "payment_mode": "Bitcoin"
        })

    def test_zero_customer_id_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "0",
            "product_id": "1",
            "quantity": "1",
            "discount_pct": "0",
            "payment_mode": "Cash"
        })

    def test_zero_product_id_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "1",
            "product_id": "0",
            "quantity": "1",
            "discount_pct": "0",
            "payment_mode": "Cash"
        })

    def test_decimal_quantity_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "1",
            "product_id": "1",
            "quantity": "1.5",
            "discount_pct": "0",
            "payment_mode": "Cash"
        })
        
    def test_nan_discount_is_rejected(self):
        self.check_invalid_sale({
            "customer_id": "1",
            "product_id": "1",
            "quantity": "1",
            "discount_pct": "nan",
            "payment_mode": "Cash"
        })

    def test_insufficient_stock_is_rejected(self):
        mock_cursor = unittest.mock.MagicMock()
        mock_cursor.rowcount = 0

        mock_context = MagicMock()
        mock_context.__enter__.return_value = (None, mock_cursor)

        with patch.object(shop, "db_cursor", return_value=mock_context) as mock_db:
            response = self.client.post(
                "/sales/add",
                data={
                    "customer_id": "1",
                    "product_id": "1",
                    "quantity": "999999",
                    "discount_pct": "0",
                    "payment_mode": "Cash"
                }
            )

        self.assertEqual(response.status_code, 302)
        mock_db.assert_called_once()
        mock_cursor.execute.assert_called_once()

    def test_valid_sale_calculation(self):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = (1000.0,)

        mock_context = MagicMock()
        mock_context.__enter__.return_value = (None, mock_cursor)

        with patch.object(
            shop, "db_cursor", return_value=mock_context
        ) as mock_db:
            response = self.client.post(
                "/sales/add",
                data={
                    "customer_id": "1",
                    "product_id": "1",
                    "quantity": "2",
                    "discount_pct": "10",
                    "payment_mode": "Cash"
                }
            )

        self.assertEqual(response.status_code, 302)
        mock_db.assert_called_once()

        self.assertEqual(mock_cursor.execute.call_count, 3)

        insert_call = mock_cursor.execute.call_args_list[2]
        sale_values = insert_call.args[1]

        self.assertEqual(sale_values[2], 2)
        self.assertEqual(sale_values[3], 10.0)
        self.assertEqual(sale_values[4], 1800.0)
        self.assertEqual(sale_values[5], "Cash")  

    def test_zero_discount_keeps_full_price(self):
        self._check_discount_total(0, 2000.0)

    def test_full_discount_makes_total_zero(self):
        self._check_discount_total(100, 0.0)

    def _check_discount_total(self, discount, expected_total):
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = (1000.0,)

        mock_context = MagicMock()
        mock_context.__enter__.return_value = (None, mock_cursor)

        with patch.object(
            shop, "db_cursor", return_value=mock_context
        ):
            response = self.client.post(
                "/sales/add",
                data={
                    "customer_id": "1",
                    "product_id": "1",
                    "quantity": "2",
                    "discount_pct": str(discount),
                    "payment_mode": "Cash"
                }
            )

        self.assertEqual(response.status_code, 302)
        sale_values = mock_cursor.execute.call_args_list[2].args[1]
        self.assertEqual(sale_values[4], expected_total)

if __name__ == "__main__":
    unittest.main(verbosity=2)