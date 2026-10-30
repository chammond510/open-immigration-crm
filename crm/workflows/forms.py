from django import forms

from .models import WorkflowTemplate
from .services import outline_text, parse_outline

OUTLINE_HELP = (
    "One phase per line, each followed by its checklist items starting with a dash. "
    'End a phase with "(due in 30 days)" to put a deadline in the work queue when it starts. '
    'Add a description to an item after "|".'
)
OUTLINE_PLACEHOLDER = (
    "Gather documents (due in 14 days)\n"
    "- First item\n"
    "- Second item | A note for staff\n"
    "Prepare\n"
    "- Next item"
)


class WorkflowTemplateForm(forms.ModelForm):
    outline = forms.CharField(
        label="Phases and checklist items",
        help_text=OUTLINE_HELP,
        widget=forms.Textarea(attrs={"rows": 16, "placeholder": OUTLINE_PLACEHOLDER}),
    )

    class Meta:
        model = WorkflowTemplate
        fields = ["name", "case_type", "description", "apply_to_new_matters", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["case_type"].choices = [
            ("", "Any case type"),
            *self.fields["case_type"].choices[1:],
        ]
        if self.instance.pk:
            self.fields["outline"].initial = outline_text(self.instance)

    def clean_outline(self):
        return parse_outline(self.cleaned_data["outline"])


class ApplyWorkflowForm(forms.Form):
    template = forms.ModelChoiceField(
        queryset=WorkflowTemplate.objects.none(), empty_label=None, label="Workflow"
    )

    def __init__(self, *args, templates=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["template"].queryset = WorkflowTemplate.objects.filter(
            pk__in=[template.pk for template in templates]
        )
        self.fields["template"].choices = [(t.pk, t.name) for t in templates]
