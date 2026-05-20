import datetime
import json
from datetime import date
from unittest import SkipTest
from unittest.mock import MagicMock, patch

from dateutil.relativedelta import relativedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import freeze_time, tagged
from odoo.tools import mute_logger

from odoo.addons.hr_expense.tests.common import TestExpenseCommon
from odoo.addons.hr_expense_stripe.controllers.main import StripeIssuingController
from odoo.addons.hr_expense_stripe.utils import format_amount_to_stripe
from odoo.addons.http_routing.tests.common import MockRequest
from odoo.addons.phone_validation.tools import phone_validation

MAKE_STRIPE_REQUEST_PROXY = 'odoo.addons.hr_expense_stripe.{}.make_request_stripe_proxy'


@freeze_time('2025-06-15')
@tagged('post_install', '-at_install')
class TestExpenseStripeCommon(TestExpenseCommon):
    @classmethod
    def setup_references(cls):
        # TO OVERRIDE
        return '', ''

    @classmethod
    def setUpClass(cls):
        cls.country_code = False
        stripe_country_ref, stripe_currency_ref = cls.setup_references()
        if not stripe_country_ref or not stripe_currency_ref:
            raise SkipTest("Test class not properly configured with country and currency references")

        super().setUpClass()

        cls.stripe_country, cls.stripe_currency = cls.env.ref(stripe_country_ref), cls.env.ref(stripe_currency_ref)
        chart_template_ref = cls.env['account.chart.template']._guess_chart_template(cls.stripe_country)
        template_vals = cls.env['account.chart.template']._get_chart_template_mapping()[chart_template_ref]
        template_module = cls.env['ir.module.module']._get(template_vals['module'])
        if template_module.state != 'installed':
            cls.chart_template = 'generic_coa'  # We want the tests to run without l10n_be installed
        cls.stripe_company_data = cls.setup_other_company(
            name='Stripe Company',
            currency_id=cls.stripe_currency.id,
            country_id=cls.stripe_country.id,
            account_fiscal_country_id=cls.stripe_country.id,
        )
        cls.env = cls.env(context={'allowed_company_ids': cls.stripe_company_data['company'].ids})
        cls.company = cls.stripe_company = cls.stripe_company_data['company']
        cls.stripe_journal = cls.env['account.chart.template'].ref('stripe_issuing_journal')

        if cls.stripe_company.account_fiscal_country_id != cls.stripe_country:
            # If the generic_coa was installed
            cls.stripe_company.write({
                'country_id': cls.stripe_country.id,
                'account_fiscal_country_id': cls.stripe_country.id,
                'stripe_currency_id': cls.stripe_currency.id,
            })
        tax_group = cls.env['account.tax.group'].create({
            'name': "Tax group for Stripe tests",
            'country_id': cls.stripe_country.id,
        })
        cls.stripe_tax = cls.env['account.tax'].create({
            'name': "Tax that is 50%",
            'company_id': cls.stripe_company.id,
            'country_id': cls.stripe_country.id,
            'tax_group_id': tax_group.id,
            'amount': 50,
        })
        cls.stripe_company.account_purchase_tax_id = cls.stripe_tax.id
        cls.stripe_company.zip = '1000'
        cls.expense_user_employee.company_ids += cls.env.company
        cls.expense_user_employee.company_id = cls.env.company
        cls.expense_user_employee.partner_id.write({
            'country_id': cls.stripe_country,
            'street': '123 Stripe St',
            'city': 'Brussels',
            'zip': '1000',
        })
        cls.expense_user_manager.company_ids += cls.env.company
        cls.expense_user_manager.company_id = cls.env.company
        cls.stripe_manager_employee = cls.env['hr.employee'].sudo().create({
            'name': 'Stripe Manager Employee',
            'user_id': cls.expense_user_manager.id,
            'company_id': cls.stripe_company.id,
        }).sudo(False)
        cls.stripe_employee = cls.env['hr.employee'].sudo().create({
            'name': 'Stripe Employee',
            'user_id': cls.expense_user_employee.id,
            'company_id': cls.stripe_company.id,
            'birthday': datetime.date(1990, 1, 1),
            'address_id': cls.expense_user_employee.partner_id.id,
            'parent_id': cls.stripe_manager_employee.id,
            'work_phone': '+32000000000',
        }).sudo(False)
        cls.iap_key_private = cls.env['certificate.key']._generate_ed25519_private_key(company=cls.stripe_company, name="IAP TEST KEY")
        cls.iap_key_public_bytes = cls.iap_key_private._get_public_key_bytes()
        cls.iap_key_public = cls.env['certificate.key'].create([{
            'name': 'IAP TEST PUBLIC KEY',
            'content': cls.iap_key_public_bytes,
            'company_id': cls.stripe_company.id,
        }])
        cls.Controller = StripeIssuingController()

        # Override the signature validation of the controller for the tests
        cls.Controller._validate_signature = MagicMock()
        cls.Controller._validate_signature.return_value = True

        cls.env['ir.config_parameter'].set_param('hr_expense_stripe.stripe_mode', 'test')  # Just to be sure
        # Set taxes belonging to the proper company on the products

        cls.env['product.mcc.stripe.tag'].search([]).product_id.supplier_taxes_id = [
            Command.set(cls.stripe_company.account_purchase_tax_id.ids),
        ]
        cls.env = cls.env(user=cls.expense_user_employee)

    #####################################
    #           Test Actions            #
    #####################################
    def test_create_account(self):
        """ Test the creation of a Stripe account for a company. By mocking the two requests made to Stripe """
        expected_calls = [
            {
                'route': 'accounts',
                'method': 'POST',
                'payload': {
                    'country': self.stripe_country.code,
                    'db_public_key': '__ignored__',
                    'db_uuid': '__ignored__',
                    'db_webhook_url': '__ignored__',
                },
                'return_data': {
                    'id': 'acct_1234567890',
                    'iap_public_key': self.iap_key_public_bytes.decode(),
                    'stripe_pk': 'stripe_public_key',
                },
            },
            {
                'route': 'account_links',
                'method': 'POST',
                'payload': {
                    'account': 'acct_1234567890',
                    'refresh_url': '__ignored__',
                    'return_url': '__ignored__',
                },
                'return_data': {
                    'url': 'WWW.SOME.STRIPE.URL',
                },
            },
        ]
        with self.patch_stripe_requests('models.res_company', expected_calls):
            self.stripe_company.with_context(skip_stripe_account_creation_commit=True)._create_stripe_account()
            return_action = self.stripe_company.action_configure_stripe_account()
            self.assertDictEqual(return_action, {'type': 'ir.actions.act_url', 'url': 'WWW.SOME.STRIPE.URL', 'target': 'self'})

        self.assertRecordValues(
            self.stripe_company,
            [{
                'stripe_id': 'acct_1234567890',
                'stripe_issuing_activated': True,
                'stripe_account_issuing_status': 'restricted',
                'stripe_account_issuing_tos_accepted': True,
                'stripe_account_issuing_tos_acceptance_date': fields.Date.context_today(self.stripe_company),
            }],
        )
        stripe_public_key = self.env['ir.config_parameter'].sudo().get_param(
            f'hr_expense_stripe.{self.stripe_company.id}_stripe_issuing_pk'
        )
        self.assertEqual('stripe_public_key', stripe_public_key)
        self.assertEqual(self.iap_key_public_bytes, self.stripe_company.stripe_issuing_iap_public_key_id.content)

    def test_configure_account(self):
        """" Test the configure account action """
        with self.assertRaises(ValidationError):
            self.stripe_company.action_configure_stripe_account()

        self.setup_account_creation()
        expected_calls = [{
            'route': 'account_links',
            'method': 'POST',
            'payload': {
                'account': 'acct_1234567890',
                'refresh_url': '__ignored__',
                'return_url': '__ignored__',
            },
            'return_data': {
                'url': 'WWW.SOME.STRIPE.URL',
            },
        }]
        # We should always be able to get the link to be redirected to the configuration part after having created the account
        with self.patch_stripe_requests('models.res_company', expected_calls):
            return_action = self.stripe_company.action_configure_stripe_account()
            self.assertDictEqual(return_action, {'type': 'ir.actions.act_url', 'url': 'WWW.SOME.STRIPE.URL', 'target': 'self'})

    def test_refresh_account(self):
        """ Test the refresh account action, and mock an account validation by Stripe """
        self.setup_account_creation()
        self.assertEqual('restricted', self.stripe_company.stripe_account_issuing_status, "Account should be restricted after creation")
        self.stripe_company.env['ir.config_parameter'].sudo().set_param(
            f'hr_expense_stripe.{self.stripe_company.id}_stripe_issuing_pk',
            'OUTDATED PUBLIC KEY',
        )
        expected_calls = [{
            'route': 'accounts/{account}',
            'method': 'GET',
            'payload': {},
            'route_params': {'account': 'acct_1234567890'},
            'return_data': {
                'id': 'acct_1234567890',
                'capabilities': {'card_issuing': 'verified'},
                'stripe_pk': 'NEW PUBLIC KEY',
            },
        }]

        with self.patch_stripe_requests('models.res_company', expected_calls):
            self.stripe_company.action_refresh_stripe_account()
            stripe_pk = self.stripe_company.env['ir.config_parameter'].sudo().get_param(
                f'hr_expense_stripe.{self.stripe_company.id}_stripe_issuing_pk'
            )
            self.assertEqual('NEW PUBLIC KEY', stripe_pk)

    def test_create_card(self):
        self.setup_account_creation(funds_amount=10000)
        with self.assertRaises(AccessError):
            # The employee user should not be able to create the card
            self.env['hr.expense.stripe.card'].with_user(self.expense_user_employee).create([{
                'employee_id': self.stripe_employee.id,
                'name': 'Test Card',
                'company_id': self.company.id,
            }])
        self.env['hr.expense.stripe.card'].with_user(self.expense_user_manager).create([{
            'employee_id': self.stripe_employee.id,
            'name': 'Test Card',
            'company_id': self.company.id,
        }])

    def test_virtual_card(self):
        self.setup_account_creation(funds_amount=10000)
        card = self.env['hr.expense.stripe.card'].with_user(self.expense_user_manager).create([{
            'employee_id': self.stripe_employee.id,
            'name': 'Test Card',
            'company_id': self.company.id,
            'card_type': 'virtual',
        }])

        expected_calls = [{
            'route': 'cardholders',
            'method': 'POST',
            'payload': '__ignored__',
            'return_data': {
                'id': 'ich_1234567890',
                'livemode': False,
            },
        }]
        with self.patch_stripe_requests('wizard.hr_expense_stripe_cardholder_wizard', expected_calls):
            self.create_cardholder(card, self.stripe_employee)

        expected_calls = [{
            'route': 'cards',
            'method': 'POST',
            'payload': {
                'cardholder': 'ich_1234567890',
                'currency': self.stripe_currency.name,
                'type': 'virtual',
            },
            'return_data': {
                'id': 'ic_1234567890',
                'status': 'active',
                'cancellation_reason': None,
                'last4': '1234',
                'exp_month': 12,
                'exp_year': 2125,
                'livemode': False,
            },
        }]
        with self.patch_stripe_requests('models.hr_expense_stripe_card', expected_calls):
            with self.assertRaises(AccessError):
                # The employee user should not be able to activate the card
                card.with_user(self.expense_user_employee).action_activate_card()
            card.with_user(self.expense_user_manager).action_activate_card()
        self.assertRecordValues(card, [
            {'stripe_id': 'ic_1234567890', 'state': 'active', 'last_4': '1234', 'expiration': '12/25'},
        ])

    def test_create_cardholder_no_user(self):
        """ Test that creating a cardholder for an employee without user raises an error """
        self.setup_account_creation(funds_amount=10000)
        new_employee = self.env['hr.employee'].sudo().create([{
            'name': 'Employee Without User',
            'company_id': self.company.id,
        }]).sudo(False)
        with self.assertRaises(ValidationError):
            self.env['hr.expense.stripe.card'].with_user(self.expense_user_manager).create([{
                'employee_id': new_employee.id,
                'name': 'Test Card',
                'company_id': self.company.id,
            }])

    def test_create_cardholder(self):
        """ Test the creation of the cardholder wizard """
        self.setup_account_creation(funds_amount=10000)
        card = self.env['hr.expense.stripe.card'].with_user(self.expense_user_manager).create([{
            'employee_id': self.stripe_employee.id,
            'name': 'Test Card',
            'company_id': self.company.id,
        }])
        with self.assertRaises(AccessError):
            # The employee user should not be able to create the cardholder
            card.with_user(self.expense_user_employee).action_open_cardholder_wizard()
        action = card.with_user(self.expense_user_manager).action_open_cardholder_wizard()
        wizard = self.env['hr.expense.stripe.cardholder.wizard'].with_context(action['context']).browse(action['res_id']).sudo()
        expected_calls = [{
            'route': 'cardholders',
            'method': 'POST',  # Even though it's technically a GET request, it generates the new data on Stripe if updated
            'payload': {
                'account': 'acct_1234567890',
                'lang': 'en_US',
                'billing': {'address': {'country': self.stripe_country.code.upper(), 'city': 'Brussels', 'line1': '123 Stripe St', 'postal_code': '1000'}},
                'name': 'Stripe Employee',
                'email': 'expense_user_employee@example.com',
                'individual': {'dob': {'day': 1, 'month': 1, 'year': 1990}, 'first_name': 'Stripe', 'last_name': 'Employee'},
                'preferred_locales': ['en'],
                'phone_number': '+32000000000',
            },
            'return_data': {
                'id': 'ich_1234567890',
                'status': 'active',
                'cancellation_reason': None,
                'last4': '1234',
                'exp_month': 12,
                'exp_year': 2125,
                'livemode': False,
            },
        }]
        with (
            self.patch_stripe_requests('wizard.hr_expense_stripe_cardholder_wizard', expected_calls),
            patch.object(phone_validation, 'phone_format', new=lambda *args, **kwargs: "+32000000000"),
        ):
            wizard.action_save_cardholder()

    #####################################
    #           Test Webhooks           #
    #####################################
    def test_webhook_balance_available_event(self):
        """ Test the journal balance update when Stripe receives the money """
        self.setup_account_creation()

        timestamp_before_event = (fields.Datetime.now() + relativedelta(days=-1)).timestamp()
        self.simulate_webhook_call(
            self.get_event_balance_updated_data(format_amount_to_stripe(10.0, self.stripe_currency))
        )
        self.assertEqual(self.stripe_journal.stripe_issuing_balance, 10.0)

        # events may be received out of order, the balance is not updated with an old event
        self.simulate_webhook_call(
            self.get_event_balance_updated_data(format_amount_to_stripe(5.0, self.stripe_currency), timestamp=timestamp_before_event)
        )
        self.assertEqual(self.stripe_journal.stripe_issuing_balance, 10.0)

        # But in order is fine
        self.simulate_webhook_call(
            self.get_event_balance_updated_data(format_amount_to_stripe(5.0, self.stripe_currency))
        )
        self.assertEqual(self.stripe_journal.stripe_issuing_balance, 5.0)

    def test_webhook_issuing_authorization_request_event(self):
        """ Test the `issuing_authorization.request` event. Focusing on the checks the card payments limit"""
        card = self.setup_account_creation(funds_amount=10000, create_active_card_for=self.stripe_employee)
        stripe_amount = format_amount_to_stripe(100, self.stripe_currency)
        auth_mcc = self.env['product.mcc.stripe.tag'].search([('code', '=', '4511')], limit=1)
        auth_mcc_product = auth_mcc.product_id
        bad_auth_mcc = self.env['product.mcc.stripe.tag'].search([
            ('code', '!=', '4511'),
            ('product_id', 'not in', [False, auth_mcc_product.id]),
        ], limit=1)

        event_data = self.get_event_expected_data()
        event_data['type'] = 'issuing_authorization.request'
        event_data['data']['object'] = {
            'id': 'iauth_12345678900',
            'object': 'issuing.authorization',
            'amount': 0,  # Pending
            'approved': False,
            'authorization_method': 'online',
            'card': {'id': card.stripe_id},
            'cardholder': 'ic_1234567890',
            'currency': self.stripe_currency.name.lower(),
            'livemode': False,
            'merchant_amount': 0,
            'merchant_currency': self.stripe_currency.name.lower(),
            'merchant_data': {
                'category': auth_mcc.stripe_name,
                'category_code': auth_mcc.code,
                'country': self.stripe_country.code.lower(),
                'name': 'Test Merchant',
                'tax_id': None,
            },
            'pending_request': {
                'amount': stripe_amount,
                'currency': self.stripe_currency.name.lower(),
                'is_amount_controllable': False,
                'merchant_amount': stripe_amount,
                'merchant_currency': self.stripe_currency.name.lower(),
            },
            'status': 'pending',
        }

        # Case 1 No limits
        card.write({
            'spending_policy_country_tag_ids': [Command.clear()],
            'spending_policy_category_tag_ids': [Command.clear()],
            'spending_policy_transaction_amount': 0,
            'spending_policy_interval_amount': 0,
            'spending_policy_interval': 'all_time',
        })
        result = self.simulate_webhook_call(event_data)
        self.assertTrue(result['content']['approved'], "The authorization should be approved")

        # Case 2 Bad Country
        card.spending_policy_country_tag_ids = [Command.set(self.env.ref('base.us').ids)]

        result = self.simulate_webhook_call(event_data)
        self.assertFalse(result['content']['approved'], "The authorization should be refused, and an expense created")
        expense = self.env['hr.expense'].search([('stripe_authorization_id', '=', 'iauth_12345678900')])
        self.assertRecordValues(expense, [
            {'total_amount': 100, 'total_amount_currency': 100, 'state': 'refused', 'product_id': auth_mcc_product.id},
        ])
        reason_start = "Your Expense Test Merchant has been refused Reason:"
        self.assertEqual(expense.message_ids[:1].preview, f"{reason_start} Country not allowed")

        # Case 3 Good Country
        card.spending_policy_country_tag_ids = [Command.set(self.stripe_country.ids)]
        result = self.simulate_webhook_call(event_data)
        self.assertTrue(result['content']['approved'], "The authorization should be approved")

        # Case 4 Bad MCC
        card.write({
            'spending_policy_country_tag_ids': [Command.clear()],
            'spending_policy_category_tag_ids': [Command.set(bad_auth_mcc.ids)],
        })
        event_data['data']['object']['id'] = 'iauth_12345678901'
        self.simulate_webhook_call(event_data)
        expense = self.env['hr.expense'].search([('stripe_authorization_id', '=', 'iauth_12345678901')])
        self.assertRecordValues(expense, [
            {'total_amount': 100, 'total_amount_currency': 100, 'state': 'refused', 'product_id': auth_mcc_product.id},
        ])
        self.assertEqual(expense.message_ids[:1].preview, f"{reason_start} MCC not allowed")

        # Case 5 Good MCC
        card.spending_policy_category_tag_ids = [Command.set(auth_mcc.ids)]
        result = self.simulate_webhook_call(event_data)
        self.assertTrue(result['content']['approved'], "The authorization should be approved")

        # Case 6 Bad Transaction Amount
        card.spending_policy_category_tag_ids = [Command.clear()]
        card.spending_policy_transaction_amount = 50.0
        event_data['data']['object']['id'] = 'iauth_12345678902'
        result = self.simulate_webhook_call(event_data)
        expense = self.env['hr.expense'].search([('stripe_authorization_id', '=', 'iauth_12345678902')])
        self.assertFalse(result['content']['approved'], "The authorization should be refused, and an expense created")
        self.assertRecordValues(expense, [
            {'total_amount': 100, 'total_amount_currency': 100, 'state': 'refused', 'product_id': auth_mcc_product.id},
        ])
        self.assertEqual(expense.message_ids[:1].preview, f"{reason_start} Transaction amount exceeds the maximum allowed")

        # Case 5 Good Transaction Amount
        card.spending_policy_transaction_amount = 150.0
        result = self.simulate_webhook_call(event_data)
        self.assertTrue(result['content']['approved'], "The authorization should be approved")

        # Case 6 Bad Interval Amount
        card.spending_policy_transaction_amount = 0
        expenses = self.create_expenses([{
            'product_id': auth_mcc_product.id,
            'total_amount_currency': 10,
            'company_id': self.company.id,
            'currency_id': self.stripe_currency.id,
            'employee_id': self.stripe_employee.id,
            'card_id': card.id,
            'mcc_tag_id': auth_mcc.id,
            'stripe_authorization_id': 'iauth_123456789',
            'stripe_transaction_id': 'ipi_123456789',
            'date': date(2025, 6, 14),
            'name': 'Previous card expense',
        },
        ]).sudo()
        expenses.flush_recordset()  # Needed to be able to get the data
        expenses.action_submit()
        expenses._do_approve()
        self.post_expenses_with_wizard(expenses, date=date(2025, 6, 15))
        event_data['data']['object']['id'] = 'iauth_12345678900'
        with freeze_time("2025-06-14 12:00:00"):
            card.spending_policy_interval_amount = 100
            card.spending_policy_interval = 'daily'
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertFalse(result['content']['approved'], "The authorization should be refused")

        with freeze_time("2025-06-15 12:00:00"):  # Next day, daily amount reset
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertTrue(result['content']['approved'], "The authorization should be approved")

            # Weekly interval, 15 is a sunday
            card.spending_policy_interval = 'weekly'
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertFalse(result['content']['approved'], "The authorization should be refused")

        with freeze_time("2025-06-16 12:00:00"):  # Next day (monday), weekly amount reset
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertTrue(result['content']['approved'], "The authorization should be approved")

            # Monthly interval
            card.spending_policy_interval = 'monthly'
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertFalse(result['content']['approved'], "The authorization should be refused")

        with freeze_time("2025-07-01 12:00:00"):
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertTrue(result['content']['approved'], "The authorization should be approved")

            # All time interval
            card.spending_policy_interval = 'all_time'
            event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
            result = self.simulate_webhook_call(event_data)
            self.assertFalse(result['content']['approved'], "The authorization should be refused")

        card.spending_policy_interval_amount = 110
        event_data['data']['object']['created'] = int(fields.Datetime.now().timestamp())
        result = self.simulate_webhook_call(event_data)
        self.assertTrue(result['content']['approved'], "The authorization should be approved")

    def test_webhook_issuing_authorization_created_event(self):
        """ Test the expense creation when receiving an `issuing_authorization.created` event """
        card = self.setup_account_creation(funds_amount=10000, create_active_card_for=self.stripe_employee)
        stripe_amount = format_amount_to_stripe(100, self.stripe_currency)
        auth_mcc = self.env['product.mcc.stripe.tag'].search([('code', '=', '4511')], limit=1)

        event_data = self.get_event_expected_data()
        event_data['type'] = 'issuing_authorization.created'
        event_data['data']['object'] = {
            'id': 'iauth_1234567890',
            'object': 'issuing.authorization',
            'amount': stripe_amount,
            'amount_details': {
                'atm_fee': None,
                'cashback_amount': 0
            },
            'approved': True,
            'card': {'id': card.stripe_id},
            'merchant_amount': stripe_amount,
            'merchant_currency': self.stripe_currency.name.lower(),
            'merchant_data': {
                'category': 'airlines_air_carriers',
                'category_code': '4511',
                'country': (self.stripe_country.code or '').upper(),
                'name': 'Test Merchant',
            },
            'pending_request': None,
            'request_history': [{
                'approved': True,
                'amount': stripe_amount,
                'reason': 'webhook_approved'
            }],
            'status': 'pending',
        }
        self.assertFalse(self.env['hr.expense'].sudo().search([('stripe_authorization_id', '=', 'iauth_1234567890')]))
        self.simulate_webhook_call(event_data)
        expense = self.env['hr.expense'].sudo().search([('stripe_authorization_id', '=', 'iauth_1234567890')])
        self.assertRecordValues(expense, [
            {'total_amount': 100.0, 'total_amount_currency': 100.0, 'state': 'draft', 'product_id': auth_mcc.product_id.id},
        ])

    def test_webhook_issuing_authorization_updated_event(self):
        card = self.setup_account_creation(funds_amount=10000, create_active_card_for=self.stripe_employee)
        stripe_amount = format_amount_to_stripe(100, self.stripe_currency)
        auth_mcc = self.env['product.mcc.stripe.tag'].search([('code', '=', '4511')], limit=1)

        event_data = self.get_event_expected_data()
        event_data['type'] = 'issuing_authorization.updated'  # Works the same way as created
        event_data['data']['object'] = {
            'id': 'iauth_1234567890',
            'object': 'issuing.authorization',
            'amount': stripe_amount,
            'amount_details': {
                'atm_fee': None,
                'cashback_amount': 0
            },
            'approved': False,
            'card': {'id': card.stripe_id},
            'merchant_amount': stripe_amount,
            'merchant_currency': self.stripe_currency.name.lower(),
            'merchant_data': {
                'category': 'airlines_air_carriers',
                'category_code': '4511',
                'country': (self.stripe_country.code or '').upper(),
                'name': 'Test Merchant',
            },
            'pending_request': None,
            'request_history': [{
                'approved': False,
                'amount': stripe_amount,
                'reason': 'webhook_timeout'
            }],
            'status': 'closed',
        }
        self.simulate_webhook_call(event_data)
        expense = self.env['hr.expense'].sudo().search([('stripe_authorization_id', '=', 'iauth_1234567890')])
        self.assertRecordValues(expense, [
            {'total_amount': 100.0, 'total_amount_currency': 100.0, 'state': 'refused', 'product_id': auth_mcc.product_id.id},
        ])

    def test_webhook_issuing_card_updated_event(self):
        """ Test the card updates when receiving an `issuing_card.updated` event (mostly for card-stop) cancellations """
        card = self.setup_account_creation(funds_amount=10000, create_active_card_for=self.stripe_employee)
        event_data = self.get_event_expected_data()
        event_data['type'] = 'issuing_card.updated'  # Works the same way as created
        event_data['data']['object'] = {
            'id': 'ic_1234567890',
            'cancellation_reason': None,
            'shipping': None,
            'status': 'inactive',
            'type': 'virtual',
        }
        self.assertRecordValues(card, [
            {'stripe_id': 'ic_1234567890', 'state': 'active'},
        ])
        self.simulate_webhook_call(event_data)
        self.assertRecordValues(card, [
            {'stripe_id': 'ic_1234567890', 'state': 'inactive'},
        ])
        event_data['data']['object'] = {
            'id': 'ic_1234567890',
            'cancellation_reason': 'none',
            'shipping': None,
            'status': 'canceled',
            'type': 'virtual',
        }
        self.simulate_webhook_call(event_data)
        self.assertRecordValues(card, [
            {'stripe_id': 'ic_1234567890', 'state': 'canceled'},
        ])

    def test_webhook_issuing_transaction_created_event(self):
        """ Test the bank statement line and expense are created
        when an `issuing_transaction.created` event is received.
        Also test that two transactions for the same authorization generates a split expense
        """
        self.setup_account_creation(funds_amount=10000, create_active_card_for=self.stripe_employee)
        auth_mcc = self.env['product.mcc.stripe.tag'].search([('code', '=', '4511')], limit=1)
        stripe_amount = format_amount_to_stripe(-500, self.stripe_currency)
        today = date(2025, 6, 15)

        event_data = self.get_event_expected_data()
        event_data['type'] = 'issuing_transaction.created'
        event_data['data']['object'] = {
            'id': 'ipi_1234567890',
            'amount': stripe_amount,
            'authorization': 'iauth_1234567890',
            'card': 'ic_1234567890',
            'created': fields.Datetime.now().timestamp(),
            'merchant_amount': stripe_amount,
            'merchant_currency': self.stripe_currency.name.lower(),
            'merchant_data': {
                'category': 'airlines_air_carriers',
                'category_code': '4511',
                'country': self.stripe_country.code.upper(),
                'name': 'Test Merchant',
            },
            'type': 'capture',
        }
        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'ipi_1234567890'),
        ])
        expense = self.env['hr.expense'].sudo().search([('stripe_transaction_id', '=', 'ipi_1234567890')])
        self.assertRecordValues(bank_statement_line, [
            {'amount': -500.0, 'date': today, 'payment_ref': 'Card ending in 1234 payment to Test Merchant', 'state': 'posted'},
        ])
        self.assertRecordValues(expense, [
            {'total_amount': 500.0, 'total_amount_currency': 500.0, 'state': 'draft', 'product_id': auth_mcc.product_id.id},
        ])

        # Test several captures for the same authorization create split expenses
        event_data['data']['object']['id'] = 'ipi_1234567891'
        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'ipi_1234567891'),
        ])
        self.assertEqual(len(bank_statement_line), 1, "A bank statement line should have have been created for the transaction")
        expense_two = self.env['hr.expense'].sudo().search([('stripe_transaction_id', '=', 'ipi_1234567891')])
        self.assertRecordValues(expense_two, [
            {'total_amount': 500.0, 'total_amount_currency': 500.0, 'split_expense_origin_id': expense.id},
        ])
        expenses = expense + expense_two
        expenses.action_submit()
        expenses._do_approve()
        self.post_expenses_with_wizard(expenses)
        self.assertRecordValues(expenses, [
            {'state': 'paid'},
            {'state': 'paid'},
        ])

        # Test refunds
        event_data['data']['object'].update({
            'id': 'ipi_1234567893',
            'amount': format_amount_to_stripe(300, self.stripe_currency),
            'type': 'refund',
        })
        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'ipi_1234567893'),
        ])
        self.assertRecordValues(bank_statement_line, [
            {'amount': 300.0, 'date': today, 'payment_ref': 'Card ending in 1234 payment to Test Merchant', 'state': 'posted'},
        ])

    def test_webhook_issuing_transaction_updated_event(self):
        """ Test the `issuing_transaction.updated` event refund is properly reflected in the bank statement line and expense """
        self.setup_account_creation(funds_amount=10000, create_active_card_for=self.stripe_employee)
        auth_mcc = self.env['product.mcc.stripe.tag'].search([('code', '=', '4511')], limit=1)
        stripe_amount = format_amount_to_stripe(-500, self.stripe_currency)
        today = date(2025, 6, 15)

        event_data = self.get_event_expected_data()
        event_data['type'] = 'issuing_transaction.created'
        event_data['data']['object'] = {
            'id': 'ipi_1234567890',
            'amount': stripe_amount,
            'authorization': 'iauth_1234567890',
            'card': 'ic_1234567890',
            'created': fields.Datetime.now().timestamp(),
            'merchant_amount': stripe_amount,
            'merchant_currency': self.stripe_currency.name.lower(),
            'merchant_data': {
                'category': 'airlines_air_carriers',
                'category_code': '4511',
                'country': self.stripe_country.code.upper(),
                'name': 'Test Merchant',
            },
            'type': 'capture',
        }
        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'ipi_1234567890'),
        ])
        expense = self.env['hr.expense'].sudo().search([('stripe_transaction_id', '=', 'ipi_1234567890')])
        self.assertRecordValues(bank_statement_line, [
            {'amount': -500.0, 'date': today, 'payment_ref': 'Card ending in 1234 payment to Test Merchant', 'state': 'posted'},
        ])
        self.assertRecordValues(expense, [
            {'total_amount': 500.0, 'total_amount_currency': 500.0, 'state': 'draft', 'product_id': auth_mcc.product_id.id},
        ])

        # Now update the transaction (e.g., partial refund)
        event_data['type'] = 'issuing_transaction.updated'
        event_data['data']['object']['amount'] = format_amount_to_stripe(-300, self.stripe_currency)
        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'ipi_1234567890'),
        ])
        self.assertRecordValues(bank_statement_line, [
            {'amount': -300.0, 'date': today, 'payment_ref': 'Card ending in 1234 payment to Test Merchant', 'state': 'posted'},
        ])
        expense = self.env['hr.expense'].sudo().search([('stripe_transaction_id', '=', 'ipi_1234567890')])
        self.assertRecordValues(expense, [
            {'total_amount': 300.0, 'total_amount_currency': 300.0, 'state': 'draft', 'product_id': auth_mcc.product_id.id},
        ])

    def test_webhook_topup_succeeded_event(self):
        """ Test that a bank statement line is created when a top-up is succeeded """
        self.setup_account_creation()
        topup_amount = 50.0
        stripe_topup_amount = format_amount_to_stripe(topup_amount, self.stripe_currency)
        event_data = self.get_event_expected_data()
        event_data['type'] = 'topup.succeeded'
        event_data['data']['object'] = {
            'id': 'tu_1234567890',
            'object': 'topup',
            'amount': stripe_topup_amount,
            'currency': self.stripe_currency.name.lower(),
            'description': 'Test Top-Up',
            'status': 'succeeded',
            'created': int(fields.Datetime.now().timestamp()),
        }

        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'tu_1234567890'),
        ])
        self.assertEqual(len(bank_statement_line), 1, "A bank statement line should have been created for the top-up")
        self.assertEqual(bank_statement_line.amount, topup_amount, "The bank statement line should have the correct amount")

        # if we receive the data again, no duplicate should be created
        self.simulate_webhook_call(event_data)
        bank_statement_line = self.env['account.bank.statement.line'].sudo().search([
            ('journal_id', '=', self.stripe_journal.id),
            ('stripe_id', '=', 'tu_1234567890'),
        ])
        self.assertEqual(len(bank_statement_line), 1, "A bank statement line should have been created for the top-up")

    #####################################
    #              HELPERS              #
    #####################################
    @classmethod
    def assertIsSubset(cls, expected, actual, msg=None, depth=0):
        """Assert that `actual` contains all key-value pairs in `expected`."""
        errors = []
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            errors.append(f"Both expected and actual must be dictionaries, got {expected.__class__} and {actual.__class__}")

        for key, value in expected.items():
            if key not in actual:
                errors.append(f'Missing key "{key}" in dict')
                continue
            if value == '__ignored__':
                # When we just want to know that the key has a value
                continue
            tested_value = actual[key]
            if isinstance(value, dict) and isinstance(tested_value, dict):
                errors += cls.assertIsSubset(value, tested_value, msg=f'Sub-dict "{key}" error', depth=depth + 1)
            elif value != tested_value:
                errors.append(f'Key "{key}" expected "{value}" as value, {actual[key]}  was found instead')

        if depth > 0:
            if errors:
                return [msg] + errors
            else:
                return []

        if errors:
            raise cls.failureException('\n'.join((msg, *errors)))
        return []

    def patched_make_request_stripe_proxy(self, expected_calls):
        def patched_inner(company, route, route_params=None, payload=None, method="POST", headers=None):
            payload = payload or {}
            if not expected_calls:
                raise ValueError(
                    f"No more calls were expected, but we received: {route} {method} with payload: {payload} for company {company.display_name}"
                )
            expected_call = expected_calls.pop(0)
            self.assertEqual(self.stripe_company.id, company.id)
            self.assertEqual(expected_call.get('route'), route, "Wrong route called")
            self.assertEqual(expected_call.get('method'), method, "Wrong method for request")
            expected_route_params = expected_call.get('route_params')
            if expected_route_params:
                self.assertIsSubset(expected_route_params, route_params, "Wrong route params")
            expected_headers = expected_call.get('headers')
            if expected_headers:
                self.assertDictEqual(expected_headers, headers, "Wrong headers")
            if expected_call.get('payload', '__ignored__') != '__ignored__':
                self.assertIsSubset(
                    expected=expected_call['payload'],
                    actual=payload,
                    msg='Create Stripe connect account request failed with the following errors:',
                )
            return expected_call['return_data']
        return patched_inner

    def patch_stripe_requests(self, method_path, expected_calls):
        """ Helper to patch the stripe request method with expected calls"""
        return patch(
            target=MAKE_STRIPE_REQUEST_PROXY.format(method_path),
            new=self.patched_make_request_stripe_proxy(expected_calls),
        )

    def setup_account_creation(self, funds_amount=0, create_active_card_for=None):
        """ Helper to create the account on the database by mocking required calls

        :param None|float funds_amount: If set, will fund the account with the given amount after creation
        :param None|hr.employee create_active_card_for: If set, will create and activate a virtual card for the given employee after creation
        """
        expected_calls = [
            {
                'route': 'accounts',
                'method': 'POST',
                'payload': {
                    'country': self.stripe_country.code.upper(),
                    'db_public_key': '__ignored__',
                    'db_uuid': '__ignored__',
                    'db_webhook_url': '__ignored__',
                },
                'return_data': {
                    'id': 'acct_1234567890',
                    'iap_public_key': self.iap_key_public_bytes.decode(),
                    'stripe_pk': 'stripe_public_key',
                },
            },
            {
                'route': 'account_links',
                'method': 'POST',
                'payload': {
                    'account': 'acct_1234567890',
                    'refresh_url': '__ignored__',
                    'return_url': '__ignored__',
                },
                'return_data': {
                    'url': 'WWW.SOME.STRIPE.URL',
                },
            },
        ]
        with self.patch_stripe_requests('models.res_company', expected_calls):
            self.stripe_company.with_context(skip_stripe_account_creation_commit=True)._create_stripe_account()  # To fix in master to use the action method
            self.stripe_company.action_configure_stripe_account()
        if funds_amount:
            self.fund_account(amount=funds_amount)
        if create_active_card_for:
            card = self.create_card(
                card_name='Test Card',
                employee=create_active_card_for,
                create_cardholder=True,
            )
            expected_calls = [{
                'route': 'cards',
                'method': 'POST',
                'payload': '__ignored__',
                'return_data': {
                    'id': 'ic_1234567890',
                    'status': 'active',
                    'type': 'virtual',
                    'cancellation_reason': None,
                    'last4': '1234',
                    'exp_month': 12,
                    'exp_year': 2125,
                    'livemode': False,
                },
            }]
            with self.patch_stripe_requests('models.hr_expense_stripe_card', expected_calls):
                card.action_activate_card()
            return card
        return self.env['hr.expense.stripe.card']

    def fund_account(self, amount=100000.0):
        """ Helper to quickly fund the account on the database by mocking required calls """
        self.simulate_webhook_call(
            self.get_event_balance_updated_data(format_amount_to_stripe(amount, self.stripe_currency))
        )
        self.simulate_webhook_call(
            self.get_event_topup_succeeded_updated_data(format_amount_to_stripe(amount, self.stripe_currency))
        )

    def create_cardholder(self, card, employee):
        """ Helper to quickly create a cardholder on the database by mocking required calls """
        action = card.sudo().action_open_cardholder_wizard()
        wizard = (
            self.env['hr.expense.stripe.cardholder.wizard']
            .with_context(action['context'])
            .with_user(self.expense_user_manager)
            .browse(action['res_id'])
        )
        wizard.phone_number = '+32000000000'

        expected_calls = [{
            'route': 'cardholders',
            'method': 'POST',  # Even though it's technically a GET request, it generates the new data on Stripe if updated
            'payload': '__ignored__',
            'return_data': {
                'id': 'ich_1234567890',
                'livemode': False,
            },
        }]
        with (
            self.patch_stripe_requests('wizard.hr_expense_stripe_cardholder_wizard', expected_calls),
            patch.object(phone_validation, 'phone_format', new=lambda *args, **kwargs: "+32000000000"),
        ):
            wizard.action_save_cardholder()

    def create_card(self, card_name='Test Card', country_ids=False, mcc_ids=False, employee=None, create_cardholder=True, **kwargs):
        """ Helper to quickly create a card on the database by mocking required calls """
        card_create_vals = {
            'employee_id': (employee or self.stripe_employee).id,
            'name': card_name,
            'company_id': self.env.company.id,
            **kwargs,
        }
        if country_ids:
            card_create_vals['spending_policy_country_tag_ids'] = [Command.set(country_ids)]
        if mcc_ids:
            card_create_vals['spending_policy_category_tag_ids'] = [Command.set(mcc_ids)]
        card = self.env['hr.expense.stripe.card'].with_user(self.expense_user_manager).create([card_create_vals])
        if create_cardholder:
            self.create_cardholder(card=card, employee=employee)
        return card

    @mute_logger('odoo.addons.hr_expense_stripe.controllers.main')
    def simulate_webhook_call(self, stripe_data, company_uuid=None):
        """ Helper to mock a webhook call to the controller """
        result = {}

        def make_json_response(data, **kwargs):
            result.update({'content': data, **kwargs})
            return json.dumps({'content': data}.update(kwargs))

        company_uuid = company_uuid or self.stripe_company.stripe_issuing_iap_webhook_uuid
        with MockRequest(self.env, path=f'/stripe_issuing/webhook/{company_uuid}') as request:
            request.httprequest.method = 'POST'
            request.httprequest.data = str(stripe_data).encode()
            request.httprequest.headers = {
                'Stripe-Signature': f'signature={self.iap_key_public_bytes.decode()}',
                'Iap-Signature': 'signature=1234',
                'Content-Type': 'application/json',
            }

            request.get_json_data = lambda: stripe_data
            request.make_json_response = make_json_response
            self.Controller.stripe_issuing_webhook(company_uuid)
        return result

    def get_event_expected_data(self, account=None, timestamp=0):
        """ helper to get the base event data structure, only contains the important fields """
        account = account or self.stripe_company.stripe_id
        return {
            'id': f'evt_{account}1234567890',
            'account': account,
            'object': 'event',
            'api_version': '2025-01-27.acacia',
            'created': timestamp or datetime.datetime.now().timestamp(),
            'data': {
                'object': {}  # TO FILL
            },
            'livemode': False,
            'pending_webhooks': 1,
            'request': {'id': False, 'idempotency_key': False},
            'type': False,  # TO FILL
        }

    def get_event_balance_updated_data(self, amount, account=None, currency=None, timestamp=0):
        """ helper to get a `balance.available` event data structure, only contains the important fields """
        account = account or self.stripe_company.stripe_id
        currency = currency or self.stripe_currency.name
        event_data = self.get_event_expected_data(account, timestamp)
        event_data['type'] = 'balance.available'
        event_data['data']['object'] = {
            'object': 'balance',
            'available': [{'amount': 0, 'currency': currency, 'source_types': {}}],
            'instant_available': [{'amount': 0, 'currency': currency, 'source_types': {}}],
            'issuing': {'available': [{'amount': amount, 'currency': currency}]},
            'livemode': False,
            'pending': [{'amount': 0, 'currency': currency, 'source_types': {}}],
            'refund_and_dispute_prefunding': {
                'available': [{'amount': 0, 'currency': currency}],
                'pending': [{'amount': 0, 'currency': currency}],
            }
        }
        return event_data

    def get_event_topup_succeeded_updated_data(self, amount, account=None, currency=None, timestamp=0):
        """ helper to get a `topup.succeeded` event data structure, only contains the important fields """
        account = account or self.stripe_company.stripe_id
        currency = currency or self.stripe_currency.name
        event_data = self.get_event_expected_data(account, timestamp)
        event_data['type'] = 'topup.succeeded'
        event_data['data']['object'] = {
            'id': 'tu_12345678901234567890',
            'object': 'topup',
            'amount': amount,
            'balance_transaction': 'txn_12345678901234567890',
            'created': timestamp,
            'currency': currency,
            'expected_availability_date': timestamp,
            'livemode': False,
            'metadata': {},
            'source': {},
            'statement_descriptor': None,
            'status': 'succeeded',
            'transfer_group': None,
            'destination_balance': 'issuing',
        }
        return event_data
