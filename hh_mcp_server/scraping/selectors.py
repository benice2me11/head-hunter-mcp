# === Vacancy Search Results ===
VACANCY_CARD = "[data-qa='serp-item'], [data-qa='vacancy-serp__vacancy']"
VACANCY_TITLE = "[data-qa='serp-item__title'], [data-qa='vacancy-serp__vacancy-title']"
VACANCY_SALARY = "[data-qa='vacancy-serp__vacancy-compensation'], [data-qa='serp-item__compensation']"
VACANCY_EMPLOYER = "[data-qa='vacancy-serp__vacancy-employer'], [data-qa='serp-item__company-name']"
VACANCY_ADDRESS = "[data-qa='vacancy-serp__vacancy-address'], [data-qa='serp-item__location']"
VACANCY_SNIPPET_RESP = "[data-qa='vacancy-serp__vacancy_snippet_responsibility']"
VACANCY_SNIPPET_REQ = "[data-qa='vacancy-serp__vacancy_snippet_requirement']"
PAGER_NEXT = "[data-qa='pager-next']"
SEARCH_RESULT_COUNT = "[data-qa='vacancies-total-found']"

# === Vacancy Detail Page ===
DETAIL_TITLE = "[data-qa='vacancy-title']"
DETAIL_SALARY = "[data-qa='vacancy-salary'], [data-qa='vacancy-salary-compensation-type-net'], [data-qa='vacancy-salary-compensation-type-gross']"
DETAIL_EMPLOYER = "[data-qa='vacancy-company-name']"
DETAIL_EXPERIENCE = "[data-qa='vacancy-experience']"
DETAIL_EMPLOYMENT = "[data-qa='common-employment-text']"
DETAIL_DESCRIPTION = "[data-qa='vacancy-description']"
DETAIL_SKILLS = "[data-qa='skills-element'], [data-qa='bloko-tag bloko-tag_inline']"
DETAIL_WORK_FORMAT = "[data-qa='work-formats-text']"

# === Apply Flow ===
APPLY_BUTTON = "[data-qa='vacancy-response-link-top'], [data-qa='vacancy-response-button']"
APPLY_FORM = "[data-qa='vacancy-response-popup-form']"
RESUME_SELECT = "[data-qa='vacancy-response-popup-form-resume-dropdown']"
SELECTED_RESUME_TITLE = "[role='dialog'] [data-qa='resume-title']"
RESUME_VISIBILITY_WARNING = "[data-qa='hidden-resume-warning']"
COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle'], [data-qa='add-cover-letter']"
COVER_LETTER_INPUT = "[data-qa='vacancy-response-popup-form-letter-input']"
SUBMIT_BUTTON = "[data-qa='vacancy-response-submit-popup']"
ALREADY_APPLIED = "[data-qa='vacancy-response-link-view-topic']"

# === Apply Questions ===
QUESTION_ITEM = "[data-qa='task-body'] .vacancy-questions-item, [data-qa='vacancy-response-popup-form'] .vacancy-questions-item"
QUESTION_LABEL = ".vacancy-questions-item__title, label"
QUESTION_INPUT = "input, textarea, select"

# === Resume ===
RESUME_CARD = "[data-qa='resume']"
RESUME_TITLE_LINK = "[data-qa^='resume-card-link'], [data-qa='resume-title-link']"
RESUME_TITLE = "[data-qa='resume-title']"

# === Resume Edit / Read-back ===
# Verified against current hh.ru live DOM reports dated 2026-08-30..2026-09-09.
RESUME_DISPLAY_HEADLINE = "[data-qa='resume-block-title-position']"
RESUME_DISPLAY_SALARY = "[data-qa='resume-block-salary']"
RESUME_POSITION_FIELDS = "[data-qa^='resume-position-field-']"
RESUME_CONTACTS = "[data-qa^='resume-contact-']"
RESUME_ABOUT_CARD = "[data-qa='resume-about-card']"
RESUME_ABOUT_EDITOR = "[data-qa='resume-editor-about']"
RESUME_POSITION_FORM = "[data-qa='resume-edit-position-form']"
RESUME_HEADLINE_INPUT = "[data-qa='resume-edit-title-suggest']"
RESUME_PARTIAL_EDIT_SAVE = "[data-qa='resume-partial-edit-save']"
RESUME_PARTIAL_EDIT_CANCEL = "[data-qa='resume-partial-edit-cancel']"

RESUME_SKILLS_CARD = "[data-qa='skills-card']"
RESUME_SKILLS_DISPLAY_TAG = "[data-qa='skills-card'] [data-qa^='skill-tag-']"
RESUME_SKILLS_LEVEL_TITLE = "[data-qa^='skill-level-title-']"
RESUME_SKILLS_TAG_IN_GROUP = "[data-qa^='skill-tag-']"
RESUME_SKILLS_CHIP = "[data-qa^='chips-trigger-chip-']"
RESUME_SKILLS_CHIP_INPUT = "[data-qa='chips-trigger-input']"
RESUME_SKILLS_INPUT = "[data-qa='resume-editor-skills-input']"
RESUME_SKILLS_SUGGEST_USER_INPUT = "[data-qa='suggest-item-user-input']"

RESUME_EXPERIENCE_CARD = "[data-qa='resume-list-card-experience']"
RESUME_EXPERIENCE_VIEW_CARD = "[data-qa='profile-experience-company-card']"
RESUME_EXPERIENCE_EDIT_BUTTONS = "[data-qa^='edit-experience-button-']:not([data-qa$='-svg'])"
RESUME_EXPERIENCE_DESCRIPTION = "[data-qa='resume-editor-experience-description-input']"
RESUME_EXPERIENCE_COMPANY_TPL = "[data-qa~='resume-profile-experience-specific-company-input-{index}']"
RESUME_EXPERIENCE_POSITION_TPL = "[data-qa~='resume-profile-experience-specific-position-input-{index}']"
RESUME_EXPERIENCE_START_YEAR = "[data-qa='resume-editor-experience-start-year-input']"
RESUME_EXPERIENCE_END_YEAR = "[data-qa='resume-editor-experience-end-year-input']"
RESUME_EXPERIENCE_MONTH = "[data-qa='magritte-select-activator']"
RESUME_EXPERIENCE_SAVE = "[data-qa='profile-layout-save-button']"
RESUME_EXPERIENCE_VALIDATION_ERROR = "[data-qa='form-helper-error']"

# === Responses/Negotiations ===
RESPONSE_ITEM = "[data-qa='negotiations-item']"
RESPONSE_VACANCY_LINK = "[data-qa='negotiations-item-title']"
RESPONSE_STATUS = "[data-qa='negotiations-item-status']"
