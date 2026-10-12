"""Stock-first product shell. Presentation only; existing owned APIs execute orders.

Research publications never acquire approval because of navigation or styling.
The approved-order review remains the sole buy action offered by this workspace.
"""
from pathlib import Path
import re

_ASSETS = Path(__file__).with_name('product_assets')
CSS = (_ASSETS / 'workspace.css').read_text()
JS = (_ASSETS / 'workspace.js').read_text()


def _link(page, label, path):
    return (f'<a href="#{page}" data-t="{page}" onclick="go(\'{page}\');return false">'
            f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{path}"/></svg>'
            f'<span>{label}</span></a>')


def apply_shell(html):
    links = [
        ('ideas', 'Ideas', 'M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8L9 16h6l.6-2.2A6 6 0 0 0 12 3z'),
        ('positions', 'Portfolio', 'M3 6h18v14H3zM8 6V3h8v3M3 11h18'),
        ('orders', 'Orders', 'M4 6h16M4 12h16M4 18h10'),
        ('watch', 'Saved', 'M6 3h12v18l-6-4-6 4z'),
        ('indices', 'Markets', 'M3 19h18M5 15l5-6 4 3 6-8'),
    ]
    navigation = ''.join(_link(*row) for row in links)
    header = ('<header class="product-header"><div class="product-header-inner">'
              '<button class="product-brand" onclick="go(\'ideas\')" aria-label="OpenStocks ideas">'
              'OpenStocks<span>.</span></button><nav class="product-nav" aria-label="Main navigation">'
              + navigation + '</nav><div id="productUtilities" class="product-utilities"></div>'
              '</div></header>')
    html, count = re.subn(r'<nav class=side id=side>.*?</nav>', lambda _: header,
                         html, count=1, flags=re.S)
    if count != 1:
        raise ValueError('Product navigation anchor missing')
    bottom = ('<nav class="nav product-bottom" aria-label="Mobile navigation">'
              + ''.join(_link(*row) for row in links[:4])
              + _link('account', 'Account', 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-8 16-8 16 0')
              + '</nav>')
    html, count = re.subn(r'<nav class=nav>.*?</nav>', lambda _: bottom,
                         html, count=1, flags=re.S)
    if count != 1:
        raise ValueError('Product mobile navigation anchor missing')
    return html
