"""PAP-specific templates used by the render stage after a Decision.

Four templates: enrollment intake form (the artifact patients / providers
send in), approval letter, denial letter, missing-info letter. Uploaded
through the existing Quire design-time flow (upload → freeze → publish) so
they're first-class TemplateVersion records rather than a parallel storage
layer.

This directory holds the source .docx / .pdf files under version control;
they get uploaded during demo-tenant seeding, not imported at runtime.
"""
