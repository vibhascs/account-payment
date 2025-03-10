# Copyright 2011-2012 7 i TRIA <http://www.7itria.cat>
# Copyright 2011-2012 Avanzosc <http://www.avanzosc.com>
# Copyright 2013 Tecnativa - Pedro M. Baeza
# Copyright 2014 Markus Schneider <markus.schneider@initos.com>
# Copyright 2016 Tecnativa - Carlos Dauden
# Copyright 2017 Tecnativa - Luis M. Ontalba
# Copyright 2022 NuoBiT Solutions, S.L. - Eric Antones <eantones@nuobit.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


class PaymentReturn(models.Model):
    _name = "payment.return"
    _inherit = ["mail.thread"]
    _description = "Payment return"
    _order = "date DESC, id DESC"

    company_id = fields.Many2one(
        comodel_name="res.company",
        string="Company",
        required=True,
        states={"done": [("readonly", True)], "cancelled": [("readonly", True)]},
        default=lambda self: self.env.company,
    )
    date = fields.Date(
        string="Return date",
        help="This date will be used as the account entry date.",
        states={"done": [("readonly", True)], "cancelled": [("readonly", True)]},
        default=lambda x: fields.Date.today(),
    )
    name = fields.Char(
        string="Reference",
        required=True,
        states={"done": [("readonly", True)], "cancelled": [("readonly", True)]},
        default=lambda self: self.env["ir.sequence"].next_by_code("payment.return"),
    )
    line_ids = fields.One2many(
        comodel_name="payment.return.line",
        inverse_name="return_id",
        states={"done": [("readonly", True)], "cancelled": [("readonly", True)]},
    )
    journal_id = fields.Many2one(
        comodel_name="account.journal",
        string="Bank journal",
        required=True,
        states={"done": [("readonly", True)], "cancelled": [("readonly", True)]},
    )
    move_id = fields.Many2one(
        comodel_name="account.move",
        string="Reference to the created journal entry",
        states={"done": [("readonly", True)], "cancelled": [("readonly", True)]},
    )
    total_amount = fields.Float(
        string="Total amount",
        compute="_compute_total_amount",
        readonly=True,
        store=False,
    )
    state = fields.Selection(
        selection=[
            ("draft", "Draft"),
            ("imported", "Imported"),
            ("done", "Done"),
            ("cancelled", "Cancelled"),
        ],
        string="State",
        readonly=True,
        default="draft",
        tracking=True,
    )
    auto_reconcile_failure = fields.Boolean(
        string="Automatic reconciliation failure",
        compute="_compute_auto_reconcile_failure",
        readonly=True,
        store=False,
    )

    def _compute_auto_reconcile_failure(self):
        """
        This method computes the auto_reconcile_failure field which is as flag
        allowing to detect the unreconciled "done" payment returns with
        automatic reconciliation enabled.
        """
        for rec in self:
            if rec.state == "done" and rec.journal_id.return_auto_reconcile:
                crdt_move_line = rec.move_id.line_ids.filtered(lambda l: l.credit)
                rec.auto_reconcile_failure = not crdt_move_line.reconciled
            else:
                rec.auto_reconcile_failure = False

    @api.constrains("line_ids")
    def _check_duplicate_move_line(self):
        def append_error(error_line):
            error_list.append(
                _("Payment Line: %s (%s) in Payment Return: %s")
                % (
                    ", ".join(error_line.mapped("move_line_ids.name")),
                    error_line.partner_id.name,
                    error_line.return_id.name,
                )
            )

        error_list = []
        all_move_lines = self.env["account.move.line"]
        for line in self.mapped("line_ids"):
            for move_line in line.move_line_ids:
                if move_line in all_move_lines:
                    append_error(line)
                all_move_lines |= move_line
        if (not error_list) and all_move_lines:
            duplicate_lines = self.env["payment.return.line"].search(
                [
                    ("move_line_ids", "in", all_move_lines.ids),
                    ("return_id.state", "=", "done"),
                ]
            )
            if duplicate_lines:
                for line in duplicate_lines:
                    append_error(line)
        if error_list:
            raise ValidationError(
                _("Payment reference must be unique" "\n%s") % "\n".join(error_list)
            )

    @api.depends("line_ids.amount")
    def _compute_total_amount(self):
        return_line_model = self.env["payment.return.line"]
        domain = [("return_id", "in", self.ids)]
        res = return_line_model.read_group(
            domain=domain, fields=["return_id", "amount"], groupby=["return_id"]
        )
        lines_dict = {}
        for dic in res:
            return_id = dic["return_id"][0]
            total_amount = dic["amount"]
            lines_dict[return_id] = total_amount
        for rec in self:
            rec.total_amount = lines_dict.get(rec.id, 0.0)

    def _get_move_amount(self, return_line):
        return return_line.amount

    def _prepare_invoice_returned_vals(self):
        return {"returned_payment": True}

    def unlink(self):
        if self.filtered(lambda x: x.state == "done"):
            raise UserError(_("You can not remove a payment return if state is 'Done'"))
        return super(PaymentReturn, self).unlink()

    def button_match(self):
        self.mapped("line_ids").filtered(
            lambda x: ((not x.move_line_ids) and x.reference)
        )._find_match()
        self._check_duplicate_move_line()

    def _prepare_return_move_vals(self):
        """Prepare the values for the journal entry created from the return.

        :return: Dictionary with the record values.
        """
        self.ensure_one()
        return {
            "name": "/",
            "ref": _("Return %s") % self.name,
            "journal_id": self.journal_id.id,
            "date": self.date,
            "company_id": self.company_id.id,
        }

    def _prepare_move_line(self, move, total_amount):
        self.ensure_one()
        return {
            "name": move.ref,
            "debit": 0.0,
            "credit": total_amount,
            "account_id": self.journal_id.payment_credit_account_id.id,
            "move_id": move.id,
            "journal_id": move.journal_id.id,
        }

    def _auto_reconcile(self, credit_move_line, all_move_lines, total_amount):
        """
        Reconcile the payment return if the option return_auto_reconcile is
        enabled on the journal.
        """
        self.ensure_one()
        if not self.journal_id.return_auto_reconcile:
            return
        rounding = self.env.user.company_id.currency_id.rounding
        counterpart_move_lines = self.env["account.move.line"].browse()
        for move_line in all_move_lines:
            move = move_line.move_id
            if len(move.line_ids) != 2:  # auto.reconciliation not possible
                return
            counterpart_move_lines |= move.line_ids.filtered(
                lambda line: line != move_line
            )
        if (
            counterpart_move_lines
            and float_compare(
                total_amount,
                sum(counterpart_move_lines.mapped("debit")),
                precision_rounding=rounding,
            )
            == 0
            and not any(rec.reconciled for rec in counterpart_move_lines)
        ):
            lines_to_reconcile = credit_move_line | counterpart_move_lines
            if len(lines_to_reconcile.mapped("account_id")) != 1:
                return
            lines_to_reconcile.reconcile()

    def action_confirm(self):
        self.ensure_one()
        # Check for incomplete lines
        if self.line_ids.filtered(lambda x: not x.move_line_ids):
            raise UserError(
                _("You must input all moves references in the payment return.")
            )
        invoices = self.env["account.move"]
        move_line_model = self.env["account.move.line"]
        move = self.env["account.move"].create(self._prepare_return_move_vals())
        total_amount = 0.0
        all_move_lines = self.env["account.move.line"]
        # First loop to generate the move lines and compute the total amount
        return_line_map = {}
        for return_line in self.line_ids:
            move_line2_vals = return_line._prepare_return_move_line_vals(move)
            move_line2 = move_line_model.with_context(check_move_validity=False).create(
                move_line2_vals
            )
            total_amount += move_line2.debit
            return_line_map[return_line] = move_line2
        move_line_vals = self._prepare_move_line(move, total_amount)
        # credit_move_line: credit on transfer or bank account
        credit_move_line = move_line_model.create(move_line_vals)
        move._post()
        for return_line in self.line_ids:
            move_line2 = return_line_map[return_line]
            for move_line in return_line.move_line_ids:
                # move_line: credit on customer account (from payment move)
                # returned_moves: debit on customer account (from invoice move)
                returned_moves = move_line.matched_debit_ids.mapped("debit_move_id")
                all_move_lines |= move_line
                invoices |= returned_moves.mapped("move_id")
                move_line.remove_move_reconcile()
                (move_line | move_line2).with_context(
                    check_move_validity=False
                ).reconcile()
                return_line.move_line_ids.mapped("matched_debit_ids").write(
                    {"origin_returned_move_ids": [(6, 0, returned_moves.ids)]}
                )
            if return_line.expense_amount:
                expense_lines_vals = return_line._prepare_expense_lines_vals(move)
                move_line_model.with_context(check_move_validity=False).create(
                    expense_lines_vals
                )
            extra_lines_vals = return_line._prepare_extra_move_lines(move)
            move_line_model.create(extra_lines_vals)
        # Reconcile (if option enabled)
        self._auto_reconcile(credit_move_line, all_move_lines, total_amount)
        # Write directly because we returned payments just now
        invoices.write(self._prepare_invoice_returned_vals())
        self.write({"state": "done", "move_id": move.id})
        return True

    def action_cancel(self):
        invoices = self.env["account.move"]
        for move_line in self.mapped("move_id.line_ids").filtered(
            lambda x: x.account_internal_type == "receivable"
        ):
            for partial_line in move_line.matched_credit_ids:
                invoices |= partial_line.origin_returned_move_ids.mapped("move_id")
                lines2reconcile = (
                    partial_line.origin_returned_move_ids | partial_line.credit_move_id
                )
                partial_line.credit_move_id.remove_move_reconcile()
                lines2reconcile.reconcile()
        self.move_id.filtered(lambda move: move.state == "posted").button_draft()
        self.move_id.with_context(force_delete=True).unlink()
        self.write({"state": "cancelled", "move_id": False})
        invoices.check_payment_return()

    def action_draft(self):
        self.write({"state": "draft"})
        return True


class PaymentReturnLine(models.Model):
    _name = "payment.return.line"
    _description = "Payment return lines"

    return_id = fields.Many2one(
        comodel_name="payment.return",
        string="Payment return",
        required=True,
        ondelete="cascade",
    )
    concept = fields.Char(
        string="Concept", help="Read from imported file. Only for reference."
    )
    reason_id = fields.Many2one(
        comodel_name="payment.return.reason", string="Return reason"
    )
    reason_additional_information = fields.Char(
        string="Return reason (info)", help="Additional information on return reason."
    )
    reference = fields.Char(
        string="Reference", help="Reference to match moves from related documents"
    )
    move_line_ids = fields.Many2many(
        comodel_name="account.move.line", string="Payment Reference"
    )
    date = fields.Date(string="Return date", help="Only for reference")
    partner_name = fields.Char(
        string="Partner name",
        readonly=True,
        help="Read from imported file. Only for reference.",
    )
    partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Customer",
        domain="[('customer_rank', '>', 0)]",
    )
    amount = fields.Float(
        string="Amount",
        help="Returned amount. Can be different from the move amount",
        digits="Account",
    )
    expense_account = fields.Many2one(
        comodel_name="account.account", string="Charges Account"
    )
    expense_amount = fields.Float(string="Charges Amount")
    expense_partner_id = fields.Many2one(
        comodel_name="res.partner",
        string="Charges Partner",
        domain=[("supplier_rank", ">", 0)],
    )

    def _compute_amount(self):
        for line in self:
            line.amount = sum(line.move_line_ids.mapped("credit"))

    def _get_partner_from_move(self):
        for line in self.filtered(lambda x: not x.partner_id):
            partners = line.move_line_ids.mapped("partner_id")
            if len(partners) > 1:
                raise UserError(_("All payments must be owned by the same partner"))
            line.partner_id = partners[:1].id
            line.partner_name = partners[:1].name

    @api.onchange("move_line_ids")
    def _onchange_move_line(self):
        self._compute_amount()

    @api.onchange("expense_amount")
    def _onchange_expense_amount(self):
        if self.expense_amount:
            journal = self.return_id.journal_id
            self.expense_account = journal.default_expense_account_id
            self.expense_partner_id = journal.default_expense_partner_id

    def match_invoice(self):
        for line in self:
            domain = line.partner_id and [("partner_id", "=", line.partner_id.id)] or []
            domain += [("name", "=", line.reference), ("move_type", "=", "out_invoice")]
            invoice = self.env["account.move"].search(domain)
            if invoice:
                invoice_line_ids = invoice.line_ids.filtered(
                    lambda line: line.account_id.user_type_id.type
                    in ("receivable", "payable")
                )
                payment_lines = invoice_line_ids.mapped(
                    "matched_debit_ids.debit_move_id"
                )
                payment_lines |= invoice_line_ids.mapped(
                    "matched_credit_ids.credit_move_id"
                )
                if payment_lines:
                    line.move_line_ids = payment_lines[0].ids
                    if not line.concept:
                        line.concept = _("Invoice: %s") % invoice.name

    def match_move_lines(self):
        for line in self:
            domain = line.partner_id and [("partner_id", "=", line.partner_id.id)] or []
            if line.return_id.journal_id:
                domain += [
                    ("journal_id", "=", line.return_id.journal_id.id),
                    ("move_id.move_type", "=", "entry"),
                ]
            domain.extend(
                [
                    ("account_id.internal_type", "=", "receivable"),
                    ("reconciled", "=", True),
                    "|",
                    ("name", "=", line.reference),
                    ("ref", "=", line.reference),
                ]
            )
            move_lines = self.env["account.move.line"].search(domain)
            if move_lines:
                line.move_line_ids = move_lines.ids
                if not line.concept:
                    line.concept = _("Move lines: %s") % ", ".join(
                        move_lines.mapped("name")
                    )

    def match_move(self):
        for line in self:
            domain = line.partner_id and [("partner_id", "=", line.partner_id.id)] or []
            domain += [("name", "=", line.reference), ("move_type", "=", "entry")]
            move = self.env["account.move"].search(domain)
            if move:
                line.move_line_ids = move.line_ids.filtered(
                    lambda l: (l.account_internal_type == "receivable" and l.reconciled)
                ).ids
                if not line.concept:
                    line.concept = _("Move: %s") % move.ref

    def _find_match(self):
        # we filter again to remove all ready matched lines in inheritance
        lines2match = self.filtered(lambda x: ((not x.move_line_ids) and x.reference))
        lines2match.match_invoice()

        lines2match = lines2match.filtered(
            lambda x: ((not x.move_line_ids) and x.reference)
        )
        lines2match.match_move_lines()

        lines2match = lines2match.filtered(
            lambda x: ((not x.move_line_ids) and x.reference)
        )
        lines2match.match_move()
        self._get_partner_from_move()
        self.filtered(lambda x: not x.amount)._compute_amount()

    def _prepare_return_move_line_vals(self, move):
        self.ensure_one()
        return {
            "name": _("Return %s") % self.return_id.name,
            "debit": self.return_id._get_move_amount(self),
            "credit": 0.0,
            "account_id": self.move_line_ids[0].account_id.id,
            "partner_id": self.partner_id.id,
            "journal_id": self.return_id.journal_id.id,
            "move_id": move.id,
        }

    def _prepare_expense_lines_vals(self, move):
        self.ensure_one()
        return [
            {
                "name": move.ref,
                "move_id": move.id,
                "debit": 0.0,
                "credit": self.expense_amount,
                "partner_id": self.expense_partner_id.id,
                "account_id": self.return_id.journal_id.default_account_id.id,
            },
            {
                "move_id": move.id,
                "debit": self.expense_amount,
                "name": move.ref,
                "credit": 0.0,
                "partner_id": self.expense_partner_id.id,
                "account_id": self.expense_account.id,
            },
        ]

    def _prepare_extra_move_lines(self, move):
        """Include possible extra lines in the return journal entry for other
        return concepts.

        :param self: Reference to the payment return line.
        :param move: Reference to the journal entry created for the return.
        :return: A list with dictionaries of the extra move lines to add
        """
        self.ensure_one()
        return []
