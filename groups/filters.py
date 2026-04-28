import django_filters
from .models import Group
from django.db.models import Q
from accounts.models import Account


class GroupFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method='filter_search', label="Qidirish")

    owner = django_filters.ModelChoiceFilter(
        queryset=Account.objects.none(),  # scoped per-request in __init__
        label="Akkaunt",
        empty_label="Barchasi"
    )

    o = django_filters.OrderingFilter(
        choices=(
            ('-created_at', "Eng so'nggi"),
            ('created_at', "Eng eski"),
            ('name', "Nom (A-Z)"),
            ('-name', "Nom (Z-A)"),
        ),
        label="Saralash",
        empty_label="Odatiy"
    )

    class Meta:
        model = Group
        fields = ['search', 'owner']

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.filters['owner'].queryset = Account.objects.filter(owner=user).order_by('phone_number')

    @property
    def form(self):
        f = super().form
        for field_name in f.fields:
            is_text = field_name == 'search'
            f.fields[field_name].widget.attrs.update({
                'class': 'form-control rounded-pill bg-light border-0 fw-medium'
                if is_text else
                'form-select rounded-pill bg-light border-0 fw-medium'
            })
            if field_name == 'search':
                f.fields[field_name].widget.attrs['placeholder'] = 'Guruh nomi...'
        return f

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) |
            Q(owner__phone_number__icontains=value) |
            Q(owner__username__icontains=value)
        )
