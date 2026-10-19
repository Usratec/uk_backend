import uuid
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Upper
from django.utils import timezone

from apps.core.models import TimestampedModel

# Money is rounded to whole pence everywhere.
PENNY = Decimal("0.01")


class DiscountType(models.TextChoices):
    PERCENTAGE = "percentage", "Percentage off"
    FIXED = "fixed", "Fixed amount off"


class PromoCode(TimestampedModel):
    """A discount a customer can type in at checkout."""

    code = models.CharField(max_length=50, unique=True, help_text="Stored in capitals; matching ignores case.")
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.PERCENTAGE)
    discount_value = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(PENNY)],
        help_text="A percentage (up to 100) or an amount in pounds, depending on the type.",
    )

    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Leave blank for unlimited.")
    uses_count = models.PositiveIntegerField(default=0, editable=False)

    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True, help_text="Leave blank for no end date.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            # Matching ignores case, so two codes differing only in case would be the same code.
            models.UniqueConstraint(Upper("code"), name="promo_code_unique_case_insensitive"),
            # Enforced in the database, so a value can never get in through a bulk update or the shell.
            models.CheckConstraint(
                condition=models.Q(discount_value__gt=0),
                name="promo_code_discount_value_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(discount_type=DiscountType.PERCENTAGE) | models.Q(discount_value__lte=100),
                name="promo_code_percentage_at_most_100",
            ),
        ]

    def __str__(self) -> str:
        return self.code

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    def is_within_window(self, at=None) -> bool:
        at = at or timezone.now()
        if not self.is_active:
            return False
        if self.valid_from > at:
            return False
        return not (self.valid_until and self.valid_until < at)

    def has_uses_left(self) -> bool:
        return self.max_uses is None or self.uses_count < self.max_uses

    def is_usable(self, at=None) -> bool:
        """Whether the code can be used at the given moment."""
        return self.is_within_window(at) and self.has_uses_left()

    def discount_for(self, price: Decimal) -> Decimal:
        """How much comes off this price. Never more than the price itself."""
        if self.discount_type == DiscountType.PERCENTAGE:
            discount = (price * self.discount_value / 100).quantize(PENNY)
        else:
            discount = self.discount_value
        return min(discount, price)


class OrderStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAID = "paid", "Paid"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"


class Order(TimestampedModel):
    """One purchase attempt.

    Orders are created before payment and before the customer has an account: people buy as
    guests, and the account is created afterwards from the email address captured here. The
    prices are copied in rather than read from the package, so a later price change cannot
    rewrite what somebody was charged.
    """

    # A UUID, because the id appears in URLs the customer sees and in Stripe's metadata. Sequential
    # ids would let anyone read other people's orders by counting.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        help_text="Linked once the account exists. Blank for a guest checkout that was never paid.",
    )
    package = models.ForeignKey("catalog.Package", on_delete=models.PROTECT, related_name="orders")

    email = models.EmailField(help_text="Captured at checkout and stored lowercased.")
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    status = models.CharField(max_length=20, choices=OrderStatus.choices, default=OrderStatus.PENDING)

    stripe_session_id = models.CharField(max_length=255, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)
    # Stripe's payment id. Unique, so one payment can never be credited to two orders.
    transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True)

    original_price = models.DecimalField(max_digits=8, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    final_price = models.DecimalField(max_digits=8, decimal_places=2)

    promo_code = models.ForeignKey(PromoCode, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")

    paid_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)

    # Set when this purchase created the account, so a retry still knows a welcome email is owed
    # even though the account exists by then.
    welcome_email_required = models.BooleanField(default=False)
    welcome_email_sent_at = models.DateTimeField(null=True, blank=True)
    confirmation_email_sent_at = models.DateTimeField(null=True, blank=True)
    manager_notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email"])]

    def __str__(self) -> str:
        return f"{self.email} | {self.package.name} | {self.get_status_display()}"


class StripeEvent(models.Model):
    """A webhook Stripe has already delivered.

    Stripe retries deliveries and can send the same event more than once. Recording each event id
    is what makes handling them exactly-once, so a customer is never charged twice over or sent
    two confirmation emails.
    """

    event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.event_id})"
