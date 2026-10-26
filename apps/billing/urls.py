from django.urls import path

from apps.billing import views

urlpatterns = [
    path("payments/checkout/", views.CheckoutView.as_view(), name="checkout"),
    path(
        "payments/promo-codes/validate/",
        views.PromoCodeValidateView.as_view(),
        name="promo-code-validate",
    ),
    path("payments/orders/<uuid:order_id>/", views.OrderStatusView.as_view(), name="order-status"),
    path("payments/webhook/stripe/", views.StripeWebhookView.as_view(), name="stripe-webhook"),
    path("cabinet/orders/", views.OrderHistoryView.as_view(), name="cabinet-orders"),
]
