<#--
  SRW wrapper for every Keycloak email.

  All 16 templates in base/email/html/ import this macro, so overriding this
  one file rebrands verify-address, password-reset, org-invite and the event
  notifications -- including types added in future Keycloak releases.

  Version guards:
    ltr  exists only from KC 26.2  -> ltr!true   (unguarded = NO email sends)
    url  optional from KC 26.4     -> not referenced at all

  Only variables set for EVERY email type are used (realmName, properties,
  locale). link/event/code are per-type and would break the types that omit them.

  The logo is an EXTERNAL absolute URL, never a theme resource: theme resource
  URLs embed the migration tag, and emails are archival, so on the next
  Keycloak upgrade every logo in every already-delivered mail would 404.
  Base64 data-URIs are stripped by Gmail and Outlook.
-->
<#macro emailLayout>
<#assign _lang  = (locale.language)!"en">
<#assign _dir   = (ltr!true)?then("ltr","rtl")>
<#assign _brand = (properties.brandName)!realmName>
<!DOCTYPE html>
<html lang="${_lang}" dir="${_dir}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<meta name="color-scheme" content="light">
<meta name="supported-color-schemes" content="light">
<title>${_brand}</title>
<!--[if mso]><noscript><xml><o:OfficeDocumentSettings>
<o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
<style>
  :root { color-scheme: light; supported-color-schemes: light; }
  body { margin:0 !important; padding:0 !important; width:100% !important; }
  table { border-collapse:collapse; }
  .srw-body p { margin:0 0 16px; font-size:15px; line-height:24px; color:#2a1d12; }
  .srw-body p:last-child { margin-bottom:0; }
  .srw-body a { color:#9c2832; font-weight:600; text-decoration:underline; }
  .srw-body b { color:#2a1d12; }
</style>
</head>
<body bgcolor="#f3ece0" style="margin:0;padding:0;background-color:#f3ece0;">
<div role="article" aria-roledescription="email" lang="${_lang}" dir="${_dir}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f3ece0" style="background-color:#f3ece0;">
<tr><td align="center" style="padding:32px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#fbf6ec" style="width:600px;max-width:600px;background-color:#fbf6ec;border:1px solid #dccfb6;">
<tr><td bgcolor="#9c2832" style="background-color:#9c2832;height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
<tr><td style="padding:24px 32px 8px 32px;">
<#if (properties.logoUrl)?has_content>
  <img src="${properties.logoUrl}" width="200" alt="${_brand}" style="display:block;width:200px;max-width:200px;height:auto;">
<#else>
  <span style="font-family:Georgia,'Times New Roman',serif;font-size:20px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#2a1d12;">${_brand}</span>
</#if>
</td></tr>
<#-- Inline typography here is the fallback for clients that strip <head>. -->
<tr><td class="srw-body" style="padding:16px 32px 24px 32px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:15px;line-height:24px;color:#2a1d12;">
<#nested>
</td></tr>
<tr><td style="padding:16px 32px 24px 32px;border-top:1px solid #dccfb6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:12px;line-height:18px;color:#5a4632;">
${_brand}
</td></tr>
</table>
</td></tr>
</table>
</div>
</body>
</html>
</#macro>
