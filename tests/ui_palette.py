"""Visible HTML palette assertions shared by Chromium acceptance tests."""


async def assert_monochrome(section):
    # Scan visible HTML surfaces, including nested rows, for inherited navy.
    navy = await section.evaluate('''root => [root,...root.querySelectorAll('*')].filter(el=>{
        if(el.namespaceURI!=='http://www.w3.org/1999/xhtml' || !el.getClientRects().length)return false;
        const c=getComputedStyle(el).backgroundColor.match(/[\\d.]+/g)?.map(Number);
        return c && c.length>=3 && (c.length<4 || c[3]>.3) && c[2]>c[0]+12 && c[0]<65;
    }).map(el=>el.outerHTML.slice(0,180))''')
    assert not navy, navy
    # Catch the pale legacy captions that become invisible on white.
    contrast = await section.evaluate('''root=>{
        const rgb=s=>s.match(/[\\d.]+/g)?.map(Number);
        const lum=c=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4)
            .reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
        return [...root.querySelectorAll('*')].filter(el=>{
            if(el.namespaceURI!=='http://www.w3.org/1999/xhtml'||!el.getClientRects().length||
               ![...el.childNodes].some(n=>n.nodeType===3&&n.textContent.trim())||el.closest(':disabled'))return false;
            const style=getComputedStyle(el),fg=rgb(style.color);let bg=[255,255,255];
            for(let a=el;a;a=a.parentElement){const c=rgb(getComputedStyle(a).backgroundColor);
                if(c && (c.length===3||c[3]>=.95)){bg=c;break;}}
            const ratio=(Math.max(lum(fg),lum(bg))+.05)/(Math.min(lum(fg),lum(bg))+.05);
            return ratio<4.5 && parseFloat(style.fontSize)<24;
        }).map(el=>el.tagName+'.'+el.className+': '+el.textContent.slice(0,60)+' / '+getComputedStyle(el).color+' on '+getComputedStyle(el).backgroundColor);
    }''')
    assert not contrast, contrast
