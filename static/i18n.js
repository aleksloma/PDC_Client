// PowerDataChat internationalization (i18n) system
// Supports English (en), Georgian (geo), Russian (ru).
// Scope: login.html (landing) and dashboard.html (lab) — plus the shared profile dropdown partial.
// Blog, pricing, terms, SEO landing pages intentionally stay in English only.

window.I18N_TRANSLATIONS = {
  en: {
    // Navbar
    'nav.pricing': 'Pricing',
    'nav.data_truth': 'Data Truth',
    'nav.about_us': 'About Us',

    // Profile dropdown
    'profile.profile': 'Profile',
    'profile.subscriptions': 'Subscriptions',
    'profile.logout': 'Logout',
    'profile.login': 'Login',
    'profile.language': 'Language',

    // Hero section (login.html)
    'hero.title': 'Your Personal AI Data Analyst',
    'hero.titleLine1': 'Your Personal',
    'hero.titleLine2': 'AI Data Analyst',
    'hero.titleNew': 'Chat with your Data',
    'hero.tagline': 'Upload one or more Excel files.<br>Ask questions in plain text. Get instant results.',
    'hero.highlight1': '• Analyze single or multiple Excel files together',
    'hero.highlight2': '• Get tables, charts, reports, and BI insights',
    'hero.highlight3': '• Results computed from your actual data',
    'hero.highlightNew1': 'Ask questions in plain text •',
    'hero.highlightNew2': 'Get tables, charts, reports, and BI insights •',
    'hero.highlightNew3': 'Results computed from your actual data in seconds •',
    'hero.cta_text': 'Get insights from your data',
    'hero.cta_button': 'Try for Free',
    'hero.cta_sub': 'No formulas. No pivots. Just answers.',
    'hero.enterprise_text': 'For enterprise use',
    'hero.book_demo_btn': 'Book Demo',

    // Auth modal
    'auth.login_title': 'Welcome back',
    'auth.login_subtitle': 'Sign in to continue to PowerDataChat',
    'auth.username': 'Username',
    'auth.password': 'Password',
    'auth.confirm_password': 'Confirm Password',
    'auth.full_name': 'Full Name',
    'auth.email': 'Email Address',
    'auth.login_btn': 'Login',
    'auth.register_btn': 'Sign up for PowerDataChat',
    'auth.switch_to_register_text': "Don't have an account?",
    'auth.switch_to_login_text': 'Already have an account?',
    'auth.register_link': 'Sign up',
    'auth.login_link': 'Login',
    'auth.or': 'OR',
    'auth.google_signin': 'Sign in with Google',
    'auth.google_signup': 'Sign up with Google',
    'auth.reset_password': 'Reset password',
    'auth.legacy_reset_required': 'This account existed before passwords were introduced. Please click “Reset password” — we will email you a temporary password to sign in and set your own.',

    // "What You Can Do" section (login.html)
    'section.wycd_title': 'What You Can Do',
    'section.wycd_1': 'Identify top products, customers, or categories',
    'section.wycd_2': 'Compare metrics across multiple files',
    'section.wycd_3': 'Detect trends, outliers, and missing values',
    'section.wycd_4': 'Generate charts and BI insights',
    'section.wycd_5': 'Create shareable reports',

    // "How It Works" section
    'section.hiw_title': 'How It Works',
    'section.hiw_step1': '<strong>Upload Excel files</strong> — drag and drop excel or csv files, or share it from drive',
    'section.hiw_step2': '<strong>Describe your data</strong> — add optional context about your files',
    'section.hiw_step3': '<strong>Ask questions in chat</strong> — get tables, charts, reports, analysis, and BI insights',

    // "What You Get" section
    'section.wyg_title': 'What You Get',
    'section.wyg_charts': '<strong>Charts:</strong> Bar, line, pie, scatter plots',
    'section.wyg_tables': '<strong>Data tables:</strong> Filtered, grouped, aggregated results',
    'section.wyg_insights': '<strong>BI insights:</strong> Patterns, comparisons, drivers',
    'section.wyg_reports': '<strong>Reports:</strong> Structured summaries with explanations',

    // "Why PowerDataChat" section
    'section.why_title': 'Why PowerDataChat?',
    'section.why_fast': '<strong>Fast:</strong> Get answers in seconds',
    'section.why_nocode': '<strong>No coding:</strong> Ask in plain language',
    'section.why_accurate': '<strong>Accurate:</strong> Results computed from your actual data',
    'section.why_safe': '<strong>Safe:</strong> Your original file is secure and stays unchanged',

    // "Try It Free" CTA
    'section.try_title': 'Try It Free',
    'section.try_desc': 'Upload your Excel file. Ask a question. See the results.',
    'section.try_btn': 'Get Started Free',

    // About Us modal
    'about.title': 'About PowerDataChat',
    'about.section1_title': '1. What is PowerDataChat?',
    'about.section1_p1': "PowerDataChat is your personal AI data analyst that turns Excel and CSV files into actionable insights through natural conversation. Upload your data, describe what you're looking for, and get instant charts, tables, and analysis in seconds.",
    'about.section1_p2': 'Built for business users who need fast, accurate data analysis without writing code or learning complex tools.',
    'about.section2_title': '2. Enterprise Solutions',
    'about.section2_subtitle': 'On-Premise Integration Available',
    'about.section2_intro': 'We offer secure on-premise deployment for enterprise clients who need to:',
    'about.section2_item1': 'Connect directly to company data marts and databases',
    'about.section2_item2': 'Ensure raw data never leaves your environment',
    'about.section2_item3': 'Maintain full control over data security and compliance',
    'about.section2_item4': 'Integrate with existing data infrastructure',
    'about.section2_note': '🔒 Your data stays within your walls. Only AI-generated insights are processed.',
    'about.section3_title': '3. Contact Us',
    'about.contact_linkedin': 'LinkedIn:',
    'about.contact_whatsapp': 'WhatsApp:',
    'about.contact_email': 'Email:',
    'about.contact_note': "Interested in enterprise features or on-premise deployment? Reach out and we'll set up a demo tailored to your needs.",

    // Footer
    'footer.tagline': 'DataChat • Built for fast, private data analysis',
    'footer.pricing': 'Pricing',
    'footer.terms': 'Terms',
    'footer.privacy': 'Privacy',
    'footer.blog': 'Blog',
    'footer.vlog': 'Vlog',

    // Dashboard (lab page)
    'lab.create_new': 'Create New',
    'lab.loading': 'Loading...',
    'lab.welcome_title': 'Drop your Excel / CSV Data files here<br>and start chatting to analyze it',
    'lab.flow_uploading': 'Uploading file…',
    'lab.flow_analyzing': 'Analyzing fields…',
    'lab.flow_creating': 'Creating chat…',
    'lab.invalid_file_type': 'Unsupported file type. Use .xlsx, .xls, .csv, or .tsv.',
    'lab.ai_desc_unavailable': 'AI descriptions unavailable, you can edit them later',
    'lab.ask_placeholder': 'Ask anything about your data...',
    'lab.download_report': '📊 Download Analytics',
    'lab.view_schema': 'View / Edit Descriptions',
    'lab.add_data': '➕ Add Data',
    'lab.flow_adding': 'Adding data to chat…',
    'lab.data_added': 'Data added to chat',
    'lab.refresh_item': 'Refresh with current data',
    'lab.refresh_failed': 'Refresh failed — showing previous result',
    'lab.refresh_frozen': "Data structure changed — this chart/table can't be refreshed.",
    'lab.file_already_in_chat': 'This file is already in this chat — no changes needed',
    'lab.duplicate_file_ignored': 'Duplicate file ignored',
    'lab.run_auto_analytics': 'Run Auto Analytics',
    'lab.processing': 'Processing…',
    'lab.download_presentation': 'Download Presentation',
    'lab.auto_analytics_popup_title': 'Auto Analysis Started',
    'lab.auto_analytics_popup_body': 'Auto analysis is running in the background. It can take several minutes — you\'ll be notified when it finishes. You can safely close this window.',

    // Wizard (create new chat — single step)
    'wizard.title': 'Create New Chat',
    'wizard.step1_label': 'Upload Files',
    'wizard.step1_desc': 'Upload Excel (.xlsx) or CSV/TSV files. AI will generate descriptions automatically — you can review or edit them later.',
    'wizard.step1_hint': '💡 You can drag & drop files anywhere in this window',
    'wizard.db_select': 'Select from DB',
    'wizard.db_no_match': 'No matching tables',
    'wizard.db_empty': 'No database tables yet — an administrator can register them under Data sources.',
    'wizard.db_tables_search': 'Search tables…',
    'lab.data_as_of': 'Data as of',
    'wizard.choose_files': '📁 Choose Files',
    'wizard.or': 'or',
    'wizard.share_google_sheet': '📎 Share Google Sheet',
    'wizard.or_drop': 'or drop here',
    'wizard.google_warning': '⚠️ Make sure the file is shared with "Anyone with the link can view"',
    'wizard.google_placeholder': 'Paste Google Sheets or Drive link here...',
    'wizard.share_btn': 'Share',
    'wizard.cancel_btn': 'Cancel',
    'wizard.generate_chat': 'Generate Chat',
    'wizard.add_data_title': 'Add Data To',
    'wizard.upload_btn': 'Upload',

    // File name-collision dialog (Add Data)
    'collision.title': 'File name already exists',
    'collision.body': 'A file named "{name}" already exists in this chat with different content. How do you want to proceed?',
    'collision.rename_btn': 'Upload as {name}',
    'collision.overwrite_btn': 'Overwrite existing file',
    'collision.overwrite_warning': 'If the new file has different columns/sheets, some previous charts and tables may no longer be refreshable.',
    'collision.same_columns': 'Both files appear to contain the same columns — existing charts should keep working after overwrite.',
    'collision.diff_columns': 'Different columns/sheets detected — after overwrite some previous charts and tables may not be refreshable.',

    // Schema viewer / editor
    'schema.title': 'View / Edit Descriptions',
    'schema.edit_hint': 'Click the pencil icon next to any description to edit it. Changes apply to this whole chat.',
    'schema.file_description_label': 'File description',
    'schema.no_description': 'No description yet — click the pencil to add one.',
    'schema.saved': 'Saved',
    'schema.save_failed': 'Save failed — try again',

    // Share modal
    'share.title': 'Share Chat',
    'share.emails_label': 'Share with specific emails (comma-separated)',
    'share.emails_placeholder': 'email1@example.com, email2@example.com',
    'share.emails_hint': 'The chat will appear in their "Shared with Me" list',
    'share.comment_label': 'Add a comment (optional)',
    'share.comment_placeholder': 'Add a note for the recipient...',
    'share.comment_hint': 'This comment will be sent via email notification only',
    'share.need_email': 'Please enter at least one email address',

    // Dashboards
    'dash.dashboards': 'Dashboards',
    'dash.add_new': 'Add new',
    'dash.new_title': 'New dashboard',
    'dash.name_label': 'Dashboard name',
    'dash.name_placeholder': 'Dashboard name',
    'dash.create': 'Create',
    'dash.created': 'Dashboard "{name}" created',
    'dash.cancel': 'Cancel',
    'dash.pin': 'Add to dashboard',
    'dash.pin_failed': 'Could not add to dashboard',
    'dash.search_placeholder': 'Search dashboards…',
    'dash.added': 'Added to',
    'dash.no_dashboards': 'No dashboards yet',
    'dash.no_matches': 'No matches — create a new dashboard below',
    'dash.list_failed': 'Could not load dashboards — try again',
    'dash.back': 'Back to Lab',
    'dash.refresh_all': '⟳ Refresh all',
    'dash.share': '🔗 Share',
    'dash.share_title': 'Share Dashboard',
    'dash.shared_by': 'Shared by',
    'dash.shared_ok': 'Dashboard shared',
    'dash.rename': 'Rename',
    'dash.rename_failed': 'Rename failed',
    'dash.delete': '🗑 Delete',
    'dash.delete_confirm': 'Delete this dashboard? Its tiles are removed; your chats and data are not affected.',
    'dash.delete_failed': 'Delete failed',
    'dash.remove_from_list': 'Remove from my list',
    'dash.remove_from_list_confirm': "Remove this shared dashboard from your list? The owner's dashboard is not affected.",
    'dash.empty_title': 'This dashboard is empty',
    'dash.empty_hint': 'Open a conversation in the Lab and click 📌 on any chart or table to add it here.',
    'dash.go_lab': 'Go to Lab',
    'dash.tile_description': 'Show description',
    'dash.show_data': 'Show data',
    'dash.show_code': 'Show code',
    'dash.download': 'Download',
    'dash.download_failed': 'Download failed',
    'dash.view_larger': 'View larger',
    'dash.refresh': 'Refresh with current data',
    'dash.remove_tile': 'Remove from dashboard',
    'dash.remove_failed': 'Could not remove the tile',
    'dash.refresh_failed': 'Refresh failed — showing saved version',
    'dash.refreshed_n': 'Refreshed {n} of {m} tiles',
    'dash.stale': 'Saved result shown',
    'dash.frozen_source_deleted': 'Source chat was deleted — showing last saved result',
    'dash.no_access': 'No access to the source data — showing saved result',
    'dash.table_preview': 'Showing {n} of {m} rows',

    // Rename modal
    'rename.title': 'Rename',
    'rename.label': 'New name',
    'rename.placeholder': 'Enter new name...',

    // Delete modal
    'delete.title': 'Delete Confirmation',
    'delete.message': 'Are you sure you want to delete this item?',
    'delete.warning': 'This action cannot be undone.',

    // Profile modal
    'profile.modal_title': 'Your Profile',
    'profile.subscription_title': 'Subscription Plan',
    'profile.current_plan': 'Current plan:',
    'profile.messages_today': 'Messages today:',
    'profile.info_title': 'Profile Information',
    'profile.username_label': 'Username',
    'profile.fullname_label': 'Full Name',
    'profile.email_label': 'Email',
    'profile.password_title': 'Change Password',
    'profile.google_notice': 'Password change is not available for Google accounts',
    'profile.current_pw': 'Current password',
    'profile.new_pw': 'New password',
    'profile.confirm_pw': 'Confirm new password',
    'profile.pw_mismatch': 'Passwords do not match',
    'profile.save_btn': 'Save Profile',
    'profile.change_pw_btn': 'Change Password',
    'profile.pw_enter_new': 'Please enter a new password',
    'profile.pw_changed': 'Password changed',
    'profile.pw_change_failed': 'Failed to change password',
    'profile.pw_wrong_current': 'Incorrect current password',

    // Plan cards
    'plan.basic': 'Basic',
    'plan.basic_price': 'Free',
    'plan.basic_desc': 'Perfect for getting started',
    'plan.basic_feat1': '1 file per upload',
    'plan.basic_feat2': '10 messages/day',
    'plan.basic_feat3': '1 PDF report/month',
    'plan.standard': 'Standard',
    'plan.standard_desc': 'Ideal for regular users',
    'plan.standard_feat1': '5 files per upload',
    'plan.standard_feat2': '50 messages/day',
    'plan.standard_feat3': '10 PDF reports/month',
    'plan.pro': 'Pro',
    'plan.pro_desc': 'Best for power users',
    'plan.pro_feat1': '10 files per upload',
    'plan.pro_feat2': '200 messages/day',
    'plan.pro_feat3': '20 PDF reports/month',
    'plan.enterprise': 'Enterprise',
    'plan.enterprise_price': 'Custom Pricing',
    'plan.enterprise_desc': 'Tailored for your organization',
    'plan.enterprise_feat1': 'Unlimited usage',
    'plan.enterprise_feat2': 'On-premise deployment',
    'plan.enterprise_feat3': 'Custom integrations',
    'plan.select_btn': 'Select',
    'plan.contact_btn': 'Contact Us',

    // Subscription modal
    'sub.title': 'Plan Change',

    // Common
    'common.cancel': 'Cancel',
    'common.close': 'Close',
    'common.save': 'Save',
    'common.delete': 'Delete',
    'common.rename': 'Rename',
    'common.share': 'Share',
    'common.confirm': 'Confirm',

    // Page title
    'page.title': 'AI Excel Assistant | Chat With Spreadsheets - PowerDataChat',

    // How It Works — flow diagram SVG
    'flow.step1_title': '1. Upload Excel Files',
    'flow.step1_sub': '.xlsx or .csv',
    'flow.step2_title': '2. Ask Questions',
    'flow.step2_sub': 'in plain English',
    'flow.step3_title': '3. Get Results',
    'flow.step3_sub': 'tables, charts, analysis',

    // How computation works (details)
    'hiw.tech_summary': 'How computation works (technical details)',
    'hiw.tech_1': '<strong>AI code generation:</strong> The AI generates Python code based on your query.',
    'hiw.tech_2': '<strong>Backend execution:</strong> A Python engine executes the code on your uploaded Excel files.',
    'hiw.tech_3': '<strong>Computed results:</strong> All tables, charts, and metrics are computed from real data, not generated as text by the LLM.',
    'hiw.tech_4': '<strong>Data privacy:</strong> The LLM never sees or processes raw spreadsheet data directly.',
    'hiw.tech_5': '<strong>Multi-file analysis:</strong> Upload multiple Excel files and analyze relationships between them.',

    // What You Get — output labels (SVG + details)
    'wyg.label_charts': 'Charts',
    'wyg.label_tables': 'Data Tables',
    'wyg.label_insights': 'Analytics',
    'wyg.label_reports': 'Reports',
    'wyg.examples_summary': 'See example questions and output details',
    'wyg.examples_title': 'Example questions:',
    'wyg.example_1': '"What\'s the total revenue by product category?"',
    'wyg.example_2': '"Show me a chart of monthly sales trends"',
    'wyg.example_3': '"Which customers haven\'t purchased in 90 days?"',
    'wyg.example_4': '"Compare this quarter to last quarter by region"',

    // Technical Notes
    'tech.title': 'Technical Notes',
    'tech.intro': 'Verifiable facts about how PowerDataChat works:',
    'tech.item_architecture': '<strong>Architecture:</strong> The AI generates Python code based on the user query. A backend Python engine executes the code on the uploaded Excel files.',
    'tech.item_data_handling': '<strong>Data handling:</strong> The LLM never sees or processes raw spreadsheet data directly. It only writes code.',
    'tech.item_computation': '<strong>Computation:</strong> All tables, charts, and metrics are computed from real data, not generated as text by the LLM.',
    'tech.item_multi_file': '<strong>Multi-file analysis:</strong> Users can upload multiple Excel files and analyze relationships between them.',
    'tech.item_formats': '<strong>Supported formats:</strong> .xlsx (Excel) and .csv files',
    'tech.item_outputs': '<strong>Outputs:</strong> Computed tables, charts (bar, line, pie, scatter), summaries, and structured reports',

    // FAQ
    'faq.title': 'Frequently Asked Questions',
    'faq.q1': 'What is PowerDataChat?',
    'faq.a1': 'An AI assistant for Excel files. Upload a spreadsheet. Ask questions. Get computed results.',
    'faq.q2': 'How does it work?',
    'faq.a2': 'You ask a question. The AI writes Python code. The Python engine runs it. You get the answer.',
    'faq.q3': 'What file types are supported?',
    'faq.a3': 'Excel (.xlsx) and CSV files.',
    'faq.q4': 'What outputs can I get?',
    'faq.a4': 'Charts, data tables, summaries, business analytics, and shareable reports.',
    'faq.q5': 'Why Python instead of just AI answers?',
    'faq.a5': 'AI can guess. Python computes. Results come from your actual data, not predictions.',
    'faq.q6': 'Does it handle large files?',
    'faq.a6': 'Yes. The Python engine processes data, so it scales with file size.',

    // References
    'refs.title': 'Learn More (References)',
    'refs.intro': 'Technologies and standards used by PowerDataChat:',

    // Footer
    'footer.copyright': 'PowerDataChat • Built for fast, private data analysis',
    'footer.last_updated': 'Last updated: January 15, 2026',
  },

  geo: {
    // Navbar
    'nav.pricing': 'ფასები',
    'nav.data_truth': 'რატომ ჩვენ',
    'nav.about_us': 'ჩვენ შესახებ',

    // Profile dropdown
    'profile.profile': 'პროფილი',
    'profile.subscriptions': 'გამოწერები',
    'profile.logout': 'გასვლა',
    'profile.login': 'შესვლა',
    'profile.language': 'ენა',

    // Hero section
    'hero.title': 'თქვენი პერსონალური AI მონაცემთა ანალიტიკოსი',
    'hero.titleLine1': 'თქვენი პერსონალური',
    'hero.titleLine2': 'AI მონაცემთა ანალიტიკოსი',
    'hero.titleNew': 'ესაუბრე შენს მონაცემებს',
    'hero.tagline': 'ატვირთეთ ერთი ან რამდენიმე Excel ფაილი.<br>დასვით კითხვები ჩვეულებრივი ტექსტით. მიიღეთ მყისიერი შედეგები.',
    'hero.highlight1': '• გააანალიზეთ ერთი ან რამდენიმე Excel ფაილი ერთდროულად',
    'hero.highlight2': '• მიიღე ცხრილები, გრაფიკები, ანგარიშები და BI ანალიტიკა',
    'hero.highlight3': '• შედეგები გამოთვლილია თქვენი რეალური მონაცემებიდან',
    'hero.highlightNew1': 'დასვი კითხვები ჩვეულებრივი ენით •',
    'hero.highlightNew2': 'მიიღე ცხრილები, გრაფიკები, ანგარიშები და BI ანალიტიკა •',
    'hero.highlightNew3': 'შედეგები ითვლება წამებში, თქვენს მონაცემებზე დაყრდნობით •',
    'hero.cta_text': 'ნახეთ რა ჩანს თქვენს მონაცემებში',
    'hero.cta_button': 'სცადე უფასოდ',
    'hero.cta_sub': 'ფორმულების გარეშე. რთული ცხრილების გარეშე. მხოლოდ პასუხები.',
    'hero.enterprise_text': 'ბიზნესისთვის',
    'hero.book_demo_btn': 'დაჯავშნე შეხვედრა',

    // Auth modal
    'auth.login_title': 'კეთილი იყოს თქვენი დაბრუნება',
    'auth.login_subtitle': 'შედით თქვენს ანგარიშში',
    'auth.username': 'მომხმარებლის სახელი',
    'auth.password': 'პაროლი',
    'auth.confirm_password': 'დაადასტურეთ პაროლი',
    'auth.full_name': 'სრული სახელი',
    'auth.email': 'ელ. ფოსტის მისამართი',
    'auth.login_btn': 'შესვლა',
    'auth.register_btn': 'PowerDataChat-ზე რეგისტრაცია',
    'auth.switch_to_register_text': 'არ გაქვთ ანგარიში?',
    'auth.switch_to_login_text': 'უკვე გაქვთ ანგარიში?',
    'auth.register_link': 'რეგისტრაცია',
    'auth.login_link': 'შესვლა',
    'auth.or': 'ან',
    'auth.google_signin': 'შედით Google-ით',
    'auth.google_signup': 'დარეგისტრირდით Google-ით',
    'auth.reset_password': 'პაროლის აღდგენა',
    'auth.legacy_reset_required': 'ეს ანგარიში პაროლების შემოღებამდე არსებობდა. დააჭირეთ „პაროლის აღდგენას“ — ელფოსტაზე დროებით პაროლს გამოგიგზავნით, რომლითაც შეხვალთ და საკუთარს დააყენებთ.',

    // "What You Can Do" section
    'section.wycd_title': 'რა შეგიძლია გააკეთო',
    'section.wycd_1': 'იპოვე საუკეთესო პროდუქტები, კლიენტები ან კატეგორიები',
    'section.wycd_2': 'შეადარე მეტრიკები რამდენიმე ფაილს შორის',
    'section.wycd_3': 'აღმოაჩინე ტენდენციები, ანომალიები და გამოტოვებული მნიშვნელობები',
    'section.wycd_4': 'შექმენი გრაფიკები და BI ანალიტიკა',
    'section.wycd_5': 'შექმენი გასაზიარებელი ანგარიშები',

    // "How It Works" section
    'section.hiw_title': 'როგორ მუშაობს',
    'section.hiw_step1': '<strong>ატვირთე Excel ფაილები</strong> — გადმოიტანე Excel ან CSV ფაილები, ან გააზიარე Drive-დან',
    'section.hiw_step2': '<strong>აღწერე შენი მონაცემები</strong> — დაამატე ნებისმიერი დამატებითი კონტექსტი',
    'section.hiw_step3': '<strong>დასვი კითხვები ჩატში</strong> — მიიღე ცხრილები, გრაფიკები, ანგარიშები, ანალიზი და BI ანალიტიკა',

    // "What You Get" section
    'section.wyg_title': 'რას მიიღებ',
    'section.wyg_charts': '<strong>გრაფიკები:</strong> ზოლოვანი, ხაზოვანი, წრიული, სკატერ გრაფიკები',
    'section.wyg_tables': '<strong>მონაცემთა ცხრილები:</strong> გაფილტრული, დაჯგუფებული, აგრეგირებული შედეგები',
    'section.wyg_insights': '<strong>BI ანალიტიკა:</strong> კანონზომიერებები, შედარებები, ფაქტორები',
    'section.wyg_reports': '<strong>ანგარიშები:</strong> სტრუქტურირებული რეზიუმეები განმარტებებით',

    // "Why PowerDataChat" section
    'section.why_title': 'რატომ PowerDataChat?',
    'section.why_fast': '<strong>სწრაფი:</strong> მიიღე პასუხები წამებში',
    'section.why_nocode': '<strong>კოდის გარეშე:</strong> ჰკითხე ჩვეულებრივი ენით',
    'section.why_accurate': '<strong>ზუსტი:</strong> შედეგები შენი რეალური მონაცემებიდან მოდის',
    'section.why_safe': '<strong>უსაფრთხო:</strong> შენი ფაილი დაცულია და უცვლელი რჩება',

    // "Try It Free" CTA
    'section.try_title': 'სცადე უფასოდ',
    'section.try_desc': 'ატვირთე შენი Excel ფაილი. დასვი კითხვა. ნახე შედეგი.',
    'section.try_btn': 'დაიწყე უფასოდ',

    // About Us modal
    'about.title': 'PowerDataChat-ის შესახებ',
    'about.section1_title': '1. რა არის PowerDataChat?',
    'about.section1_p1': 'PowerDataChat არის თქვენი პერსონალური AI მონაცემთა ანალიტიკოსი, რომელიც Excel და CSV ფაილებს ბუნებრივი საუბრის მეშვეობით ქმედით აღმოჩენებად გარდაქმნის. ატვირთეთ თქვენი მონაცემები, აღწერეთ რას ეძებთ და მიიღეთ მყისიერი გრაფიკები, ცხრილები და ანალიზი წამებში.',
    'about.section1_p2': 'შექმნილია ბიზნეს მომხმარებლებისთვის, რომლებსაც სჭირდებათ სწრაფი, ზუსტი მონაცემთა ანალიზი კოდის წერის ან რთული ხელსაწყოების შესწავლის გარეშე.',
    'about.section2_title': '2. საწარმოო გადაწყვეტილებები',
    'about.section2_subtitle': 'ხელმისაწვდომია On-Premise ინტეგრაცია',
    'about.section2_intro': 'ჩვენ გთავაზობთ უსაფრთხო on-premise განთავსებას იმ საწარმოო კლიენტებისთვის, რომლებსაც სჭირდებათ:',
    'about.section2_item1': 'პირდაპირი კავშირი კომპანიის მონაცემთა ბაზებთან',
    'about.section2_item2': 'უზრუნველყოფა, რომ ნედლი მონაცემები არ დატოვებს თქვენს გარემოს',
    'about.section2_item3': 'სრული კონტროლი მონაცემთა უსაფრთხოებასა და შესაბამისობაზე',
    'about.section2_item4': 'ინტეგრაცია არსებულ მონაცემთა ინფრასტრუქტურასთან',
    'about.section2_note': '🔒 თქვენი მონაცემები რჩება თქვენს კედლებში. მუშავდება მხოლოდ AI-ით გენერირებული დასკვნები.',
    'about.section3_title': '3. დაგვიკავშირდით',
    'about.contact_linkedin': 'LinkedIn:',
    'about.contact_whatsapp': 'WhatsApp:',
    'about.contact_email': 'ელ. ფოსტა:',
    'about.contact_note': 'გაინტერესებთ საწარმოო ფუნქციები ან on-premise განთავსება? დაგვიკავშირდით და ჩვენ მოვაწყობთ თქვენს საჭიროებებზე მორგებულ დემოს.',

    // Footer
    'footer.tagline': 'DataChat • შექმნილია სწრაფი, პირადი მონაცემთა ანალიზისთვის',
    'footer.pricing': 'ფასები',
    'footer.terms': 'წესები',
    'footer.privacy': 'კონფიდენციალურობა',
    'footer.blog': 'ბლოგი',
    'footer.vlog': 'ვლოგი',

    // Dashboard (lab page)
    'lab.create_new': 'ახლის შექმნა',
    'lab.loading': 'იტვირთება...',
    'lab.welcome_title': 'ჩაყარეთ თქვენი Excel / CSV ფაილები აქ<br>და დაიწყეთ ჩატით საუბარი მათ გასაანალიზებლად',
    'lab.flow_uploading': 'ფაილის ატვირთვა…',
    'lab.flow_analyzing': 'ველების ანალიზი…',
    'lab.flow_creating': 'ჩატის შექმნა…',
    'lab.invalid_file_type': 'მხარდაუჭერელი ფაილი. გამოიყენეთ .xlsx, .xls, .csv ან .tsv.',
    'lab.ai_desc_unavailable': 'AI-აღწერები მიუწვდომელია, მოგვიანებით შეგიძლიათ ჩაასწოროთ',
    'lab.ask_placeholder': 'დასვით კითხვა თქვენს მონაცემებზე...',
    'lab.download_report': '📊 ანალიტიკის ჩამოტვირთვა',
    'lab.view_schema': 'აღწერების ნახვა / რედაქტირება',
    'lab.add_data': '➕ მონაცემების დამატება',
    'lab.flow_adding': 'მონაცემები ემატება ჩატს…',
    'lab.data_added': 'მონაცემები დაემატა ჩატს',
    'lab.refresh_item': 'განახლება მიმდინარე მონაცემებით',
    'lab.refresh_failed': 'განახლება ვერ მოხერხდა — ნაჩვენებია წინა შედეგი',
    'lab.refresh_frozen': 'მონაცემთა სტრუქტურა შეიცვალა — ეს დიაგრამა/ცხრილი ვეღარ განახლდება.',
    'lab.file_already_in_chat': 'ეს ფაილი უკვე ამ ჩატშია — ცვლილება არ არის საჭირო',
    'lab.duplicate_file_ignored': 'დუბლიკატი ფაილი გამოტოვებულია',
    'lab.run_auto_analytics': 'ავტომატური ანალიზის გაშვება',
    'lab.processing': 'მუშავდება…',
    'lab.download_presentation': 'პრეზენტაციის ჩამოტვირთვა',
    'lab.auto_analytics_popup_title': 'ავტომატური ანალიზი დაიწყო',
    'lab.auto_analytics_popup_body': 'ავტომატური ანალიზი მუშაობს ფონურ რეჟიმში. ამას შესაძლოა რამდენიმე წუთი დასჭირდეს — დასრულებისას შეგატყობინებთ. ამ ფანჯრის უსაფრთხოდ დახურვა შეგიძლიათ.',

    // Wizard (single step)
    'wizard.title': 'ახალი ჩატის შექმნა',
    'wizard.step1_label': 'ფაილების ატვირთვა',
    'wizard.step1_desc': 'ატვირთეთ Excel (.xlsx) ან CSV/TSV ფაილები. AI ავტომატურად შექმნის აღწერებს — შეგიძლიათ მოგვიანებით ჩაასწოროთ.',
    'wizard.step1_hint': '💡 ფაილების გადმოწევა შეგიძლიათ ნებისმიერ ადგილას ამ ფანჯარაში',
    'wizard.db_select': 'არჩევა ბაზიდან',
    'wizard.db_no_match': 'ცხრილები ვერ მოიძებნა',
    'wizard.db_empty': 'მონაცემთა ბაზის ცხრილები ჯერ არ არის — მათი დამატება ადმინისტრატორს შეუძლია Data sources-ში.',
    'wizard.db_tables_search': 'ცხრილების ძებნა…',
    'lab.data_as_of': 'მონაცემები მდგომარეობით',
    'wizard.choose_files': '📁 ფაილების არჩევა',
    'wizard.or': 'ან',
    'wizard.share_google_sheet': '📎 Google Sheet-ის გაზიარება',
    'wizard.or_drop': 'ან გადმოიტანეთ აქ',
    'wizard.google_warning': '⚠️ დარწმუნდით, რომ ფაილი გაზიარებულია "ნებისმიერთან ვისაც აქვს ბმული"',
    'wizard.google_placeholder': 'ჩასვით Google Sheets ან Drive ბმული აქ...',
    'wizard.share_btn': 'გაზიარება',
    'wizard.cancel_btn': 'გაუქმება',
    'wizard.generate_chat': 'ჩატის შექმნა',
    'wizard.add_data_title': 'მონაცემების დამატება ჩატში',
    'wizard.upload_btn': 'ატვირთვა',

    // File name-collision dialog (Add Data)
    'collision.title': 'ფაილი ამ სახელით უკვე არსებობს',
    'collision.body': 'ფაილი "{name}" უკვე არსებობს ამ ჩატში, მაგრამ შიგთავსი განსხვავდება. როგორ გავაგრძელოთ?',
    'collision.rename_btn': 'ატვირთვა როგორც {name}',
    'collision.overwrite_btn': 'არსებული ფაილის გადაწერა',
    'collision.overwrite_warning': 'თუ ახალ ფაილს განსხვავებული სვეტები ან ფურცლები აქვს, ზოგიერთი ძველი დიაგრამა და ცხრილი შესაძლოა ვეღარ განახლდეს.',
    'collision.same_columns': 'ორივე ფაილი, როგორც ჩანს, ერთსა და იმავე სვეტებს შეიცავს — გადაწერის შემდეგ არსებული დიაგრამები სავარაუდოდ გააგრძელებენ მუშაობას.',
    'collision.diff_columns': 'აღმოჩენილია განსხვავებული სვეტები/ფურცლები — გადაწერის შემდეგ ზოგიერთი ძველი დიაგრამა და ცხრილი შესაძლოა ვეღარ განახლდეს.',

    // Schema viewer / editor
    'schema.title': 'აღწერების ნახვა / რედაქტირება',
    'schema.edit_hint': 'დააჭირეთ ფანქარს ნებისმიერი აღწერის სარედაქტირებლად. ცვლილებები შეინახება მთლიანი ჩატისთვის.',
    'schema.file_description_label': 'ფაილის აღწერა',
    'schema.no_description': 'აღწერა ჯერ არ არის — დააჭირეთ ფანქარს დასამატებლად.',
    'schema.saved': 'შენახულია',
    'schema.save_failed': 'შენახვა ვერ მოხერხდა — სცადეთ ხელახლა',

    // Share modal
    'share.title': 'ჩატის გაზიარება',
    'share.emails_label': 'გაუზიარეთ კონკრეტულ ელ.ფოსტებს (მძიმით გამოყოფილი)',
    'share.emails_placeholder': 'email1@example.com, email2@example.com',
    'share.emails_hint': 'ჩატი გამოჩნდება მათ "გაზიარებული ჩემთან" სიაში',
    'share.comment_label': 'დაამატეთ კომენტარი (არასავალდებულო)',
    'share.comment_placeholder': 'დაამატეთ შენიშვნა მიმღებისთვის...',
    'share.comment_hint': 'ეს კომენტარი გაიგზავნება მხოლოდ ელ.ფოსტით',
    'share.need_email': 'გთხოვთ, შეიყვანოთ მინიმუმ ერთი ელ.ფოსტა',

    // Dashboards
    'dash.dashboards': 'დაფები',
    'dash.add_new': 'ახლის დამატება',
    'dash.new_title': 'ახალი დაფა',
    'dash.name_label': 'დაფის სახელი',
    'dash.name_placeholder': 'დაფის სახელი',
    'dash.create': 'შექმნა',
    'dash.created': 'დაფა "{name}" შეიქმნა',
    'dash.cancel': 'გაუქმება',
    'dash.pin': 'დაფაზე დამატება',
    'dash.pin_failed': 'დაფაზე დამატება ვერ მოხერხდა',
    'dash.search_placeholder': 'დაფების ძიება…',
    'dash.added': 'დაემატა:',
    'dash.no_dashboards': 'დაფები ჯერ არ არის',
    'dash.no_matches': 'დამთხვევა არ არის — შექმენით ახალი დაფა ქვემოთ',
    'dash.list_failed': 'დაფების ჩატვირთვა ვერ მოხერხდა — სცადეთ ხელახლა',
    'dash.back': 'ლაბში დაბრუნება',
    'dash.refresh_all': '⟳ ყველას განახლება',
    'dash.share': '🔗 გაზიარება',
    'dash.share_title': 'დაფის გაზიარება',
    'dash.shared_by': 'გაზიარებულია:',
    'dash.shared_ok': 'დაფა გაზიარდა',
    'dash.rename': 'სახელის შეცვლა',
    'dash.rename_failed': 'სახელის შეცვლა ვერ მოხერხდა',
    'dash.delete': '🗑 წაშლა',
    'dash.delete_confirm': 'წავშალოთ ეს დაფა? მისი ელემენტები წაიშლება; თქვენი ჩატები და მონაცემები არ დაზარალდება.',
    'dash.delete_failed': 'წაშლა ვერ მოხერხდა',
    'dash.remove_from_list': 'ჩემი სიიდან ამოღება',
    'dash.remove_from_list_confirm': 'ამოვიღოთ ეს გაზიარებული დაფა თქვენი სიიდან? მფლობელის დაფა არ დაზარალდება.',
    'dash.empty_title': 'ეს დაფა ცარიელია',
    'dash.empty_hint': 'გახსენით საუბარი ლაბში და დააჭირეთ 📌-ს ნებისმიერ დიაგრამაზე ან ცხრილზე მის აქ დასამატებლად.',
    'dash.go_lab': 'ლაბში გადასვლა',
    'dash.tile_description': 'აღწერის ჩვენება',
    'dash.show_data': 'მონაცემების ჩვენება',
    'dash.show_code': 'კოდის ჩვენება',
    'dash.download': 'ჩამოტვირთვა',
    'dash.download_failed': 'ჩამოტვირთვა ვერ მოხერხდა',
    'dash.view_larger': 'გადიდება',
    'dash.refresh': 'განახლება მიმდინარე მონაცემებით',
    'dash.remove_tile': 'დაფიდან ამოღება',
    'dash.remove_failed': 'ელემენტის ამოღება ვერ მოხერხდა',
    'dash.refresh_failed': 'განახლება ვერ მოხერხდა — ნაჩვენებია შენახული ვერსია',
    'dash.refreshed_n': 'განახლდა {n} / {m} ელემენტი',
    'dash.stale': 'ნაჩვენებია შენახული შედეგი',
    'dash.frozen_source_deleted': 'წყარო ჩატი წაშლილია — ნაჩვენებია ბოლო შენახული შედეგი',
    'dash.no_access': 'წყარო მონაცემებზე წვდომა არ არის — ნაჩვენებია შენახული შედეგი',
    'dash.table_preview': 'ნაჩვენებია {n} / {m} მწკრივი',

    // Rename modal
    'rename.title': 'სახელის შეცვლა',
    'rename.label': 'ახალი სახელი',
    'rename.placeholder': 'შეიყვანეთ ახალი სახელი...',

    // Delete modal
    'delete.title': 'წაშლის დადასტურება',
    'delete.message': 'დარწმუნებული ხართ, რომ გსურთ ამ ელემენტის წაშლა?',
    'delete.warning': 'ეს მოქმედება შეუქცევადია.',

    // Profile modal
    'profile.modal_title': 'თქვენი პროფილი',
    'profile.subscription_title': 'გამოწერის გეგმა',
    'profile.current_plan': 'მიმდინარე გეგმა:',
    'profile.messages_today': 'დღეს გაგზავნილი შეტყობინებები:',
    'profile.info_title': 'პროფილის ინფორმაცია',
    'profile.username_label': 'მომხმარებლის სახელი',
    'profile.fullname_label': 'სრული სახელი',
    'profile.email_label': 'ელ. ფოსტა',
    'profile.password_title': 'პაროლის შეცვლა',
    'profile.google_notice': 'პაროლის შეცვლა არ არის ხელმისაწვდომი Google ანგარიშებისთვის',
    'profile.current_pw': 'მიმდინარე პაროლი',
    'profile.new_pw': 'ახალი პაროლი',
    'profile.confirm_pw': 'დაადასტურეთ ახალი პაროლი',
    'profile.pw_mismatch': 'პაროლები არ ემთხვევა',
    'profile.save_btn': 'პროფილის შენახვა',
    'profile.change_pw_btn': 'პაროლის შეცვლა',
    'profile.pw_enter_new': 'შეიყვანეთ ახალი პაროლი',
    'profile.pw_changed': 'პაროლი შეიცვალა',
    'profile.pw_change_failed': 'პაროლის შეცვლა ვერ მოხერხდა',
    'profile.pw_wrong_current': 'მიმდინარე პაროლი არასწორია',

    // Plan cards
    'plan.basic': 'Basic',
    'plan.basic_price': 'უფასო',
    'plan.basic_desc': 'შესანიშნავი დასაწყებად',
    'plan.basic_feat1': '1 ფაილი ატვირთვაზე',
    'plan.basic_feat2': '10 შეტყობინება/დღე',
    'plan.basic_feat3': '1 PDF ანგარიში/თვე',
    'plan.standard': 'Standard',
    'plan.standard_desc': 'იდეალური რეგულარული მომხმარებლებისთვის',
    'plan.standard_feat1': '5 ფაილი ატვირთვაზე',
    'plan.standard_feat2': '50 შეტყობინება/დღე',
    'plan.standard_feat3': '10 PDF ანგარიში/თვე',
    'plan.pro': 'Pro',
    'plan.pro_desc': 'საუკეთესო პროფესიონალი მომხმარებლებისთვის',
    'plan.pro_feat1': '10 ფაილი ატვირთვაზე',
    'plan.pro_feat2': '200 შეტყობინება/დღე',
    'plan.pro_feat3': '20 PDF ანგარიში/თვე',
    'plan.enterprise': 'Enterprise',
    'plan.enterprise_price': 'ინდივიდუალური ფასი',
    'plan.enterprise_desc': 'მორგებული თქვენი ორგანიზაციისთვის',
    'plan.enterprise_feat1': 'შეუზღუდავი გამოყენება',
    'plan.enterprise_feat2': 'On-premise განთავსება',
    'plan.enterprise_feat3': 'ინდივიდუალური ინტეგრაციები',
    'plan.select_btn': 'არჩევა',
    'plan.contact_btn': 'დაგვიკავშირდით',

    // Subscription modal
    'sub.title': 'გეგმის შეცვლა',

    // Common
    'common.cancel': 'გაუქმება',
    'common.close': 'დახურვა',
    'common.save': 'შენახვა',
    'common.delete': 'წაშლა',
    'common.rename': 'სახელის შეცვლა',
    'common.share': 'გაზიარება',
    'common.confirm': 'დადასტურება',

    // Page title
    'page.title': 'AI Excel ასისტენტი | ესაუბრეთ თქვენს ცხრილებს — PowerDataChat',

    // How It Works — flow diagram SVG
    'flow.step1_title': '1. ატვირთეთ Excel ფაილები',
    'flow.step1_sub': '.xlsx ან .csv',
    'flow.step2_title': '2. დასვით კითხვები',
    'flow.step2_sub': 'ბუნებრივი ენით',
    'flow.step3_title': '3. მიიღეთ შედეგი',
    'flow.step3_sub': 'ცხრილები, გრაფიკები, ანალიზი',

    // How computation works (details)
    'hiw.tech_summary': 'როგორ ითვლება (ტექნიკური დეტალები)',
    'hiw.tech_1': '<strong>AI კოდის გენერაცია:</strong> AI წერს Python კოდს თქვენი კითხვის მიხედვით.',
    'hiw.tech_2': '<strong>სერვერული შესრულება:</strong> Python ძრავა უშვებს კოდს თქვენს ატვირთულ Excel ფაილებზე.',
    'hiw.tech_3': '<strong>გამოთვლილი შედეგი:</strong> ყველა ცხრილი, გრაფიკი და მეტრიკა გამოთვლილია რეალური მონაცემებიდან — AI ტექსტურად არაფერს ქმნის.',
    'hiw.tech_4': '<strong>კონფიდენციალურობა:</strong> AI არასდროს ხედავს თქვენი ცხრილის ნედლ მონაცემებს.',
    'hiw.tech_5': '<strong>ბევრი ფაილის ანალიზი:</strong> ატვირთეთ რამდენიმე Excel ფაილი ერთდროულად და იპოვეთ მათ შორის კავშირები.',

    // What You Get — output labels
    'wyg.label_charts': 'გრაფიკები',
    'wyg.label_tables': 'ცხრილები',
    'wyg.label_insights': 'ანალიტიკა',
    'wyg.label_reports': 'ანგარიშები',
    'wyg.examples_summary': 'ნახე კითხვების მაგალითები და შედეგები',
    'wyg.examples_title': 'მაგალითები:',
    'wyg.example_1': '"რა არის ჯამური შემოსავალი პროდუქტის კატეგორიის მიხედვით?"',
    'wyg.example_2': '"მაჩვენე გრაფიკი თვიური გაყიდვების დინამიკის შესახებ"',
    'wyg.example_3': '"რომელ კლიენტებს 90 დღეზე მეტია არ უყიდიათ?"',
    'wyg.example_4': '"შეადარე ეს კვარტალი წინა კვარტალს რეგიონების მიხედვით"',

    // Technical Notes
    'tech.title': 'ტექნიკური შენიშვნები',
    'tech.intro': 'შემოწმებადი ფაქტები PowerDataChat-ის მუშაობის შესახებ:',
    'tech.item_architecture': '<strong>არქიტექტურა:</strong> AI წერს Python კოდს მომხმარებლის კითხვის მიხედვით. სერვერული Python ძრავა უშვებს ამ კოდს ატვირთულ Excel ფაილებზე.',
    'tech.item_data_handling': '<strong>მონაცემთა დამუშავება:</strong> AI არასდროს ხედავს ცხრილის ნედლ მონაცემებს — მხოლოდ კოდს წერს.',
    'tech.item_computation': '<strong>გამოთვლა:</strong> ცხრილები, გრაფიკები და მეტრიკები გამოითვლება რეალური მონაცემებიდან — AI ტექსტურად არაფერს აფანტავებს.',
    'tech.item_multi_file': '<strong>ბევრი ფაილის ანალიზი:</strong> მომხმარებლებს შეუძლიათ რამდენიმე Excel ფაილის ატვირთვა და მათ შორის კავშირების გაანალიზება.',
    'tech.item_formats': '<strong>მხარდაჭერილი ფორმატები:</strong> .xlsx (Excel) და .csv',
    'tech.item_outputs': '<strong>შედეგები:</strong> გამოთვლილი ცხრილები, გრაფიკები (bar, line, pie, scatter), შემაჯამებელი და სტრუქტურირებული ანგარიშები',

    // FAQ
    'faq.title': 'ხშირად დასმული კითხვები',
    'faq.q1': 'რა არის PowerDataChat?',
    'faq.a1': 'AI ასისტენტი Excel ფაილებისთვის. ატვირთეთ ცხრილი, დასვით კითხვები, მიიღეთ გამოთვლილი შედეგები.',
    'faq.q2': 'როგორ მუშაობს?',
    'faq.a2': 'თქვენ დასვამთ კითხვას. AI წერს Python კოდს. Python ძრავა უშვებს მას. თქვენ მიიღებთ პასუხს.',
    'faq.q3': 'რა ფორმატის ფაილებია მხარდაჭერილი?',
    'faq.a3': 'Excel (.xlsx) და CSV ფაილები.',
    'faq.q4': 'რა შედეგებს მივიღებ?',
    'faq.a4': 'გრაფიკები, ცხრილები, შემაჯამებლები, ბიზნეს-ანალიტიკა და გასაზიარებელი ანგარიშები.',
    'faq.q5': 'რატომ Python და არა პირდაპირ AI პასუხები?',
    'faq.a5': 'AI-ს შეუძლია შეცდეს. Python-ი ითვლის. შედეგები მოდის თქვენი რეალური მონაცემებიდან, და არა ვარაუდებიდან.',
    'faq.q6': 'დიდ ფაილებს ამუშავებს?',
    'faq.a6': 'დიახ. Python ძრავა ამუშავებს მონაცემებს, ამიტომ ეფექტურად მუშაობს ნებისმიერი ზომის ფაილზე.',

    // References
    'refs.title': 'გაიგეთ მეტი (წყაროები)',
    'refs.intro': 'ტექნოლოგიები და სტანდარტები, რომლებსაც იყენებს PowerDataChat:',

    // Footer
    'footer.copyright': 'PowerDataChat • შექმნილია სწრაფი და კონფიდენციალური მონაცემთა ანალიზისთვის',
    'footer.last_updated': 'ბოლო განახლება: 2026 წლის 15 იანვარი',
  },

  ru: {
    // Navbar
    'nav.pricing': 'Цены',
    'nav.data_truth': 'Почему мы',
    'nav.about_us': 'О нас',

    // Profile dropdown
    'profile.profile': 'Профиль',
    'profile.subscriptions': 'Подписки',
    'profile.logout': 'Выйти',
    'profile.login': 'Войти',
    'profile.language': 'Язык',

    // Hero section
    'hero.title': 'Ваш персональный AI аналитик данных',
    'hero.titleLine1': 'Ваш персональный',
    'hero.titleLine2': 'AI аналитик данных',
    'hero.titleNew': 'Общайтесь с вашими данными',
    'hero.tagline': 'Загрузите один или несколько Excel файлов.<br>Задавайте вопросы обычным текстом. Получайте мгновенные результаты.',
    'hero.highlight1': '• Анализируйте один или несколько Excel файлов вместе',
    'hero.highlight2': '• Получайте таблицы, графики, отчёты и бизнес-аналитику',
    'hero.highlight3': '• Результаты рассчитаны на основе ваших реальных данных',
    'hero.highlightNew1': 'Задавайте вопросы обычным текстом •',
    'hero.highlightNew2': 'Получайте таблицы, графики, отчёты и бизнес-аналитику •',
    'hero.highlightNew3': 'Результаты рассчитаны на основе ваших реальных данных за секунды •',
    'hero.cta_text': 'Узнайте, что скрыто в ваших данных',
    'hero.cta_button': 'Попробовать бесплатно',
    'hero.cta_sub': 'Без формул. Без сводных таблиц. Только ответы.',
    'hero.enterprise_text': 'Для бизнеса',
    'hero.book_demo_btn': 'Заказать демо',

    // Auth modal
    'auth.login_title': 'С возвращением',
    'auth.login_subtitle': 'Войдите, чтобы продолжить в PowerDataChat',
    'auth.username': 'Имя пользователя',
    'auth.password': 'Пароль',
    'auth.confirm_password': 'Подтвердите пароль',
    'auth.full_name': 'Полное имя',
    'auth.email': 'Электронная почта',
    'auth.login_btn': 'Войти',
    'auth.register_btn': 'Зарегистрироваться в PowerDataChat',
    'auth.switch_to_register_text': 'Нет аккаунта?',
    'auth.switch_to_login_text': 'Уже есть аккаунт?',
    'auth.register_link': 'Зарегистрироваться',
    'auth.login_link': 'Войти',
    'auth.or': 'ИЛИ',
    'auth.google_signin': 'Войти через Google',
    'auth.google_signup': 'Зарегистрироваться через Google',
    'auth.reset_password': 'Сбросить пароль',
    'auth.legacy_reset_required': 'Эта учётная запись существовала до введения паролей. Нажмите «Сбросить пароль» — мы отправим на вашу почту временный пароль, чтобы войти и задать свой собственный.',

    // "What You Can Do" section
    'section.wycd_title': 'Что вы можете делать',
    'section.wycd_1': 'Определять лучшие продукты, клиентов или категории',
    'section.wycd_2': 'Сравнивать метрики между несколькими файлами',
    'section.wycd_3': 'Обнаруживать тренды, выбросы и пропущенные значения',
    'section.wycd_4': 'Создавать графики и бизнес-аналитику',
    'section.wycd_5': 'Создавать отчёты для обмена',

    // "How It Works" section
    'section.hiw_title': 'Как это работает',
    'section.hiw_step1': '<strong>Загрузите Excel файлы</strong> — перетащите Excel или CSV файлы, или поделитесь из Drive',
    'section.hiw_step2': '<strong>Опишите ваши данные</strong> — добавьте необязательный контекст о файлах',
    'section.hiw_step3': '<strong>Задавайте вопросы в чате</strong> — получайте таблицы, графики, отчёты, анализ и бизнес-выводы',

    // "What You Get" section
    'section.wyg_title': 'Что вы получаете',
    'section.wyg_charts': '<strong>Графики:</strong> Столбчатые, линейные, круговые, точечные',
    'section.wyg_tables': '<strong>Таблицы данных:</strong> Отфильтрованные, сгруппированные, агрегированные результаты',
    'section.wyg_insights': '<strong>Бизнес-аналитика:</strong> Закономерности, сравнения, факторы влияния',
    'section.wyg_reports': '<strong>Отчёты:</strong> Структурированные сводки с пояснениями',

    // "Why PowerDataChat" section
    'section.why_title': 'Почему PowerDataChat?',
    'section.why_fast': '<strong>Быстро:</strong> Получайте ответы за секунды',
    'section.why_nocode': '<strong>Без кода:</strong> Задавайте вопросы простым языком',
    'section.why_accurate': '<strong>Точно:</strong> Результаты рассчитаны из ваших реальных данных',
    'section.why_safe': '<strong>Безопасно:</strong> Ваш оригинальный файл защищён и остаётся неизменным',

    // "Try It Free" CTA
    'section.try_title': 'Попробуйте бесплатно',
    'section.try_desc': 'Загрузите ваш Excel файл. Задайте вопрос. Посмотрите результаты.',
    'section.try_btn': 'Начать бесплатно',

    // About Us modal
    'about.title': 'О PowerDataChat',
    'about.section1_title': '1. Что такое PowerDataChat?',
    'about.section1_p1': 'PowerDataChat — это ваш персональный AI аналитик данных, который превращает Excel и CSV файлы в практические выводы через естественный диалог. Загрузите свои данные, опишите, что вы ищете, и получите мгновенные графики, таблицы и анализ за секунды.',
    'about.section1_p2': 'Создан для бизнес-пользователей, которым нужен быстрый, точный анализ данных без написания кода или изучения сложных инструментов.',
    'about.section2_title': '2. Корпоративные решения',
    'about.section2_subtitle': 'Доступна On-Premise интеграция',
    'about.section2_intro': 'Мы предлагаем безопасное on-premise развёртывание для корпоративных клиентов, которым необходимо:',
    'about.section2_item1': 'Подключаться напрямую к корпоративным хранилищам и базам данных',
    'about.section2_item2': 'Обеспечивать, чтобы сырые данные никогда не покидали вашу среду',
    'about.section2_item3': 'Сохранять полный контроль над безопасностью данных и соответствием требованиям',
    'about.section2_item4': 'Интегрироваться с существующей инфраструктурой данных',
    'about.section2_note': '🔒 Ваши данные остаются внутри вашей компании. Обрабатываются только AI-выводы.',
    'about.section3_title': '3. Связаться с нами',
    'about.contact_linkedin': 'LinkedIn:',
    'about.contact_whatsapp': 'WhatsApp:',
    'about.contact_email': 'Email:',
    'about.contact_note': 'Интересуют корпоративные функции или on-premise развёртывание? Свяжитесь с нами, и мы организуем демо, адаптированное под ваши нужды.',

    // Footer
    'footer.tagline': 'DataChat • Создан для быстрого, приватного анализа данных',
    'footer.pricing': 'Цены',
    'footer.terms': 'Условия',
    'footer.privacy': 'Конфиденциальность',
    'footer.blog': 'Блог',
    'footer.vlog': 'Влог',

    // Dashboard (lab page)
    'lab.create_new': 'Создать новый',
    'lab.loading': 'Загрузка...',
    'lab.welcome_title': 'Перетащите ваши Excel / CSV файлы сюда<br>и начните общаться с ними для анализа',
    'lab.flow_uploading': 'Загрузка файла…',
    'lab.flow_analyzing': 'Анализ полей…',
    'lab.flow_creating': 'Создание чата…',
    'lab.invalid_file_type': 'Неподдерживаемый формат. Используйте .xlsx, .xls, .csv или .tsv.',
    'lab.ai_desc_unavailable': 'AI-описания недоступны, вы сможете отредактировать их позже',
    'lab.ask_placeholder': 'Задайте вопрос о ваших данных...',
    'lab.download_report': '📊 Скачать аналитику',
    'lab.view_schema': 'Просмотр / редактирование описаний',
    'lab.add_data': '➕ Добавить данные',
    'lab.flow_adding': 'Добавление данных в чат…',
    'lab.data_added': 'Данные добавлены в чат',
    'lab.refresh_item': 'Обновить по текущим данным',
    'lab.refresh_failed': 'Не удалось обновить — показан предыдущий результат',
    'lab.refresh_frozen': 'Структура данных изменилась — этот график/таблицу нельзя обновить.',
    'lab.file_already_in_chat': 'Этот файл уже есть в этом чате — изменения не требуются',
    'lab.duplicate_file_ignored': 'Дубликат файла пропущен',
    'lab.run_auto_analytics': 'Запустить авто-анализ',
    'lab.processing': 'Обработка…',
    'lab.download_presentation': 'Скачать презентацию',
    'lab.auto_analytics_popup_title': 'Авто-анализ запущен',
    'lab.auto_analytics_popup_body': 'Авто-анализ выполняется в фоновом режиме. Это может занять несколько минут — мы сообщим, когда он завершится. Это окно можно безопасно закрыть.',

    // Wizard (single step)
    'wizard.title': 'Создать новый чат',
    'wizard.step1_label': 'Загрузить файлы',
    'wizard.step1_desc': 'Загрузите Excel (.xlsx) или CSV/TSV файлы. AI создаст описания автоматически — вы сможете изменить их позже.',
    'wizard.step1_hint': '💡 Вы можете перетащить файлы в любое место этого окна',
    'wizard.db_select': 'Выбрать из БД',
    'wizard.db_no_match': 'Таблицы не найдены',
    'wizard.db_empty': 'Таблиц базы данных пока нет — администратор может добавить их в разделе Data sources.',
    'wizard.db_tables_search': 'Поиск таблиц…',
    'lab.data_as_of': 'Данные по состоянию на',
    'wizard.choose_files': '📁 Выбрать файлы',
    'wizard.or': 'или',
    'wizard.share_google_sheet': '📎 Поделиться Google Sheet',
    'wizard.or_drop': 'или перетащите сюда',
    'wizard.google_warning': '⚠️ Убедитесь, что файл доступен "Всем, у кого есть ссылка"',
    'wizard.google_placeholder': 'Вставьте ссылку Google Sheets или Drive сюда...',
    'wizard.share_btn': 'Поделиться',
    'wizard.cancel_btn': 'Отмена',
    'wizard.generate_chat': 'Создать чат',
    'wizard.add_data_title': 'Добавить данные в',
    'wizard.upload_btn': 'Загрузить',

    // File name-collision dialog (Add Data)
    'collision.title': 'Файл с таким именем уже существует',
    'collision.body': 'Файл "{name}" уже есть в этом чате, но его содержимое отличается. Как поступить?',
    'collision.rename_btn': 'Загрузить как {name}',
    'collision.overwrite_btn': 'Перезаписать существующий файл',
    'collision.overwrite_warning': 'Если в новом файле другие столбцы или листы, некоторые прежние графики и таблицы могут перестать обновляться.',
    'collision.same_columns': 'Оба файла, похоже, содержат одинаковые столбцы — существующие графики должны продолжить работать после перезаписи.',
    'collision.diff_columns': 'Обнаружены различия в столбцах/листах — после перезаписи некоторые прежние графики и таблицы могут не обновляться.',

    // Schema viewer / editor
    'schema.title': 'Просмотр / редактирование описаний',
    'schema.edit_hint': 'Нажмите карандаш рядом с любым описанием, чтобы изменить его. Изменения сохраняются для всего чата.',
    'schema.file_description_label': 'Описание файла',
    'schema.no_description': 'Описания пока нет — нажмите карандаш, чтобы добавить.',
    'schema.saved': 'Сохранено',
    'schema.save_failed': 'Не удалось сохранить — попробуйте ещё раз',

    // Share modal
    'share.title': 'Поделиться чатом',
    'share.emails_label': 'Поделиться с конкретными email (через запятую)',
    'share.emails_placeholder': 'email1@example.com, email2@example.com',
    'share.emails_hint': 'Чат появится в их списке "Поделено со мной"',
    'share.comment_label': 'Добавить комментарий (необязательно)',
    'share.comment_placeholder': 'Добавьте заметку для получателя...',
    'share.comment_hint': 'Этот комментарий будет отправлен только в email уведомлении',
    'share.need_email': 'Пожалуйста, введите хотя бы один email',

    // Dashboards
    'dash.dashboards': 'Дашборды',
    'dash.add_new': 'Добавить новый',
    'dash.new_title': 'Новый дашборд',
    'dash.name_label': 'Название дашборда',
    'dash.name_placeholder': 'Название дашборда',
    'dash.create': 'Создать',
    'dash.created': 'Дашборд "{name}" создан',
    'dash.cancel': 'Отмена',
    'dash.pin': 'Добавить на дашборд',
    'dash.pin_failed': 'Не удалось добавить на дашборд',
    'dash.search_placeholder': 'Поиск дашбордов…',
    'dash.added': 'Добавлено в',
    'dash.no_dashboards': 'Дашбордов пока нет',
    'dash.no_matches': 'Совпадений нет — создайте новый дашборд ниже',
    'dash.list_failed': 'Не удалось загрузить дашборды — попробуйте ещё раз',
    'dash.back': 'Назад в Лабораторию',
    'dash.refresh_all': '⟳ Обновить все',
    'dash.share': '🔗 Поделиться',
    'dash.share_title': 'Поделиться дашбордом',
    'dash.shared_by': 'Поделился:',
    'dash.shared_ok': 'Дашборд отправлен',
    'dash.rename': 'Переименовать',
    'dash.rename_failed': 'Не удалось переименовать',
    'dash.delete': '🗑 Удалить',
    'dash.delete_confirm': 'Удалить этот дашборд? Его плитки будут удалены; ваши чаты и данные не пострадают.',
    'dash.delete_failed': 'Не удалось удалить',
    'dash.remove_from_list': 'Убрать из моего списка',
    'dash.remove_from_list_confirm': 'Убрать этот общий дашборд из вашего списка? Дашборд владельца не пострадает.',
    'dash.empty_title': 'Этот дашборд пуст',
    'dash.empty_hint': 'Откройте диалог в Лаборатории и нажмите 📌 на любом графике или таблице, чтобы добавить их сюда.',
    'dash.go_lab': 'Перейти в Лабораторию',
    'dash.tile_description': 'Показать описание',
    'dash.show_data': 'Показать данные',
    'dash.show_code': 'Показать код',
    'dash.download': 'Скачать',
    'dash.download_failed': 'Не удалось скачать',
    'dash.view_larger': 'Увеличить',
    'dash.refresh': 'Обновить по текущим данным',
    'dash.remove_tile': 'Убрать с дашборда',
    'dash.remove_failed': 'Не удалось убрать плитку',
    'dash.refresh_failed': 'Обновление не удалось — показана сохранённая версия',
    'dash.refreshed_n': 'Обновлено {n} из {m} плиток',
    'dash.stale': 'Показан сохранённый результат',
    'dash.frozen_source_deleted': 'Исходный чат удалён — показан последний сохранённый результат',
    'dash.no_access': 'Нет доступа к исходным данным — показан сохранённый результат',
    'dash.table_preview': 'Показано {n} из {m} строк',

    // Rename modal
    'rename.title': 'Переименовать',
    'rename.label': 'Новое имя',
    'rename.placeholder': 'Введите новое имя...',

    // Delete modal
    'delete.title': 'Подтверждение удаления',
    'delete.message': 'Вы уверены, что хотите удалить этот элемент?',
    'delete.warning': 'Это действие нельзя отменить.',

    // Profile modal
    'profile.modal_title': 'Ваш профиль',
    'profile.subscription_title': 'Тарифный план',
    'profile.current_plan': 'Текущий план:',
    'profile.messages_today': 'Сообщений сегодня:',
    'profile.info_title': 'Информация профиля',
    'profile.username_label': 'Имя пользователя',
    'profile.fullname_label': 'Полное имя',
    'profile.email_label': 'Email',
    'profile.password_title': 'Изменить пароль',
    'profile.google_notice': 'Изменение пароля недоступно для Google аккаунтов',
    'profile.current_pw': 'Текущий пароль',
    'profile.new_pw': 'Новый пароль',
    'profile.confirm_pw': 'Подтвердите новый пароль',
    'profile.pw_mismatch': 'Пароли не совпадают',
    'profile.save_btn': 'Сохранить профиль',
    'profile.change_pw_btn': 'Изменить пароль',
    'profile.pw_enter_new': 'Введите новый пароль',
    'profile.pw_changed': 'Пароль изменён',
    'profile.pw_change_failed': 'Не удалось изменить пароль',
    'profile.pw_wrong_current': 'Неверный текущий пароль',

    // Plan cards
    'plan.basic': 'Basic',
    'plan.basic_price': 'Бесплатно',
    'plan.basic_desc': 'Идеально для начала',
    'plan.basic_feat1': '1 файл на загрузку',
    'plan.basic_feat2': '10 сообщений/день',
    'plan.basic_feat3': '1 PDF отчёт/месяц',
    'plan.standard': 'Standard',
    'plan.standard_desc': 'Идеально для регулярных пользователей',
    'plan.standard_feat1': '5 файлов на загрузку',
    'plan.standard_feat2': '50 сообщений/день',
    'plan.standard_feat3': '10 PDF отчётов/месяц',
    'plan.pro': 'Pro',
    'plan.pro_desc': 'Лучшее для опытных пользователей',
    'plan.pro_feat1': '10 файлов на загрузку',
    'plan.pro_feat2': '200 сообщений/день',
    'plan.pro_feat3': '20 PDF отчётов/месяц',
    'plan.enterprise': 'Enterprise',
    'plan.enterprise_price': 'Индивидуальная цена',
    'plan.enterprise_desc': 'Адаптировано под вашу организацию',
    'plan.enterprise_feat1': 'Неограниченное использование',
    'plan.enterprise_feat2': 'On-premise развёртывание',
    'plan.enterprise_feat3': 'Индивидуальные интеграции',
    'plan.select_btn': 'Выбрать',
    'plan.contact_btn': 'Связаться',

    // Subscription modal
    'sub.title': 'Смена плана',

    // Common
    'common.cancel': 'Отмена',
    'common.close': 'Закрыть',
    'common.save': 'Сохранить',
    'common.delete': 'Удалить',
    'common.rename': 'Переименовать',
    'common.share': 'Поделиться',
    'common.confirm': 'Подтвердить',

    // Page title
    'page.title': 'AI-ассистент для Excel | Общайтесь с таблицами — PowerDataChat',

    // How It Works — flow diagram SVG
    'flow.step1_title': '1. Загрузите Excel',
    'flow.step1_sub': '.xlsx или .csv',
    'flow.step2_title': '2. Задайте вопрос',
    'flow.step2_sub': 'простым языком',
    'flow.step3_title': '3. Получите ответ',
    'flow.step3_sub': 'таблицы, графики, анализ',

    // How computation works (details)
    'hiw.tech_summary': 'Как устроены вычисления (технические детали)',
    'hiw.tech_1': '<strong>Генерация кода ИИ:</strong> ИИ пишет Python-код по вашему запросу.',
    'hiw.tech_2': '<strong>Выполнение на сервере:</strong> Python-движок запускает код на загруженных Excel-файлах.',
    'hiw.tech_3': '<strong>Вычисленные результаты:</strong> Все таблицы, графики и метрики рассчитаны из реальных данных, а не придуманы моделью.',
    'hiw.tech_4': '<strong>Конфиденциальность:</strong> ИИ не видит и не обрабатывает сырые данные таблиц напрямую.',
    'hiw.tech_5': '<strong>Анализ нескольких файлов:</strong> Загружайте несколько Excel-файлов и находите связи между ними.',

    // What You Get — output labels
    'wyg.label_charts': 'Графики',
    'wyg.label_tables': 'Таблицы',
    'wyg.label_insights': 'Аналитика',
    'wyg.label_reports': 'Отчёты',
    'wyg.examples_summary': 'Примеры вопросов и результатов',
    'wyg.examples_title': 'Примеры вопросов:',
    'wyg.example_1': '"Какая общая выручка по категориям продуктов?"',
    'wyg.example_2': '"Покажи график месячных продаж"',
    'wyg.example_3': '"Какие клиенты не покупали последние 90 дней?"',
    'wyg.example_4': '"Сравни этот квартал с прошлым по регионам"',

    // Technical Notes
    'tech.title': 'Технические заметки',
    'tech.intro': 'Проверяемые факты о том, как работает PowerDataChat:',
    'tech.item_architecture': '<strong>Архитектура:</strong> ИИ пишет Python-код по запросу пользователя. Серверный Python-движок запускает этот код на загруженных Excel-файлах.',
    'tech.item_data_handling': '<strong>Обработка данных:</strong> ИИ никогда не видит сырые данные таблицы — он только пишет код.',
    'tech.item_computation': '<strong>Вычисления:</strong> Все таблицы, графики и метрики рассчитаны из реальных данных, а не сгенерированы моделью как текст.',
    'tech.item_multi_file': '<strong>Анализ нескольких файлов:</strong> Пользователи могут загружать несколько Excel-файлов и анализировать связи между ними.',
    'tech.item_formats': '<strong>Поддерживаемые форматы:</strong> .xlsx (Excel) и .csv',
    'tech.item_outputs': '<strong>Результаты:</strong> вычисленные таблицы, графики (bar, line, pie, scatter), сводки и структурированные отчёты',

    // FAQ
    'faq.title': 'Часто задаваемые вопросы',
    'faq.q1': 'Что такое PowerDataChat?',
    'faq.a1': 'AI-ассистент для Excel-файлов. Загрузите таблицу, задайте вопрос, получите вычисленные результаты.',
    'faq.q2': 'Как это работает?',
    'faq.a2': 'Вы задаёте вопрос. ИИ пишет Python-код. Python его выполняет. Вы получаете ответ.',
    'faq.q3': 'Какие форматы поддерживаются?',
    'faq.a3': 'Excel (.xlsx) и CSV.',
    'faq.q4': 'Что я могу получить?',
    'faq.a4': 'Графики, таблицы, сводки, бизнес-аналитику и отчёты для обмена.',
    'faq.q5': 'Почему Python, а не просто ответы ИИ?',
    'faq.a5': 'ИИ может ошибиться. Python считает точно. Результаты берутся из ваших реальных данных, а не из предположений.',
    'faq.q6': 'Справляется с большими файлами?',
    'faq.a6': 'Да. Python-движок обрабатывает данные, поэтому масштабируется под размер файла.',

    // References
    'refs.title': 'Узнать больше (источники)',
    'refs.intro': 'Технологии и стандарты, которые использует PowerDataChat:',

    // Footer
    'footer.copyright': 'PowerDataChat • Создан для быстрого и приватного анализа данных',
    'footer.last_updated': 'Последнее обновление: 15 января 2026',
  }
};

window.i18n = {
  currentLang: (function() {
    try { return localStorage.getItem('pdc_lang') || 'en'; }
    catch (e) { return 'en'; }
  })(),

  t(key) {
    const table = window.I18N_TRANSLATIONS[this.currentLang] || window.I18N_TRANSLATIONS.en;
    if (table && table[key] != null) return table[key];
    const fallback = window.I18N_TRANSLATIONS.en;
    return (fallback && fallback[key] != null) ? fallback[key] : key;
  },

  setLang(lang) {
    if (!window.I18N_TRANSLATIONS[lang]) lang = 'en';
    this.currentLang = lang;
    try { localStorage.setItem('pdc_lang', lang); } catch (e) {}
    this.applyAll();
  },

  applyAll() {
    // textContent — also works for SVG <text> elements
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      el.textContent = this.t(key);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      el.placeholder = this.t(key);
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      el.title = this.t(key);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
      const key = el.getAttribute('data-i18n-html');
      el.innerHTML = this.t(key);
    });
    // aria-label — keeps screen-reader text in sync with the visible label
    document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
      const key = el.getAttribute('data-i18n-aria-label');
      el.setAttribute('aria-label', this.t(key));
    });
    // Document title (browser tab)
    const titleKey = document.documentElement.getAttribute('data-i18n-title-key');
    if (titleKey) {
      const translated = this.t(titleKey);
      if (translated && translated !== titleKey) document.title = translated;
    }
    // Update <html lang="..."> — internal key "geo" maps to standard HTML code "ka".
    document.documentElement.lang =
      this.currentLang === 'geo' ? 'ka' :
      this.currentLang === 'ru' ? 'ru' : 'en';
    this.adjustFontSizes();
    // Notify any listeners (e.g. dynamic content renderers)
    window.dispatchEvent(new CustomEvent('languageChanged', { detail: { lang: this.currentLang } }));
  },

  adjustFontSizes() {
    document.querySelectorAll('[data-i18n-autofit]').forEach(el => {
      const parent = el.parentElement;
      if (!parent) return;
      const maxWidth = parent.offsetWidth;
      if (!maxWidth) return;
      // Reset any previous override first so we measure the natural size
      el.style.fontSize = '';
      const originalSize = parseFloat(window.getComputedStyle(el).fontSize) || 16;
      let size = originalSize;
      let guard = 60;
      while (el.scrollWidth > maxWidth && size > originalSize * 0.7 && guard-- > 0) {
        size -= 0.5;
        el.style.fontSize = size + 'px';
      }
    });
  },

  init() {
    this.applyAll();
  }
};
