<!-- jQuery (déjà présent) -->
<script src="https://code.jquery.com/jquery-3.5.1.min.js"></script>

<!-- Bootstrap Bundle (déjà présent) -->
<script src="https://cdn.jsdelivr.net/npm/bootstrap@4.5.3/dist/js/bootstrap.bundle.min.js"></script>

<!-- jQuery Easing plugin (NÉCESSAIRE pour `easeInOutExpo`) -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery-easing/1.4.1/jquery.easing.min.js"></script>

<!-- SB Admin 2 script -->
<script src="{{ url_for('static', filename='sb-admin-2.min.js') }}"></script>

<!-- Custom script -->
<script src="{{ url_for('static', filename='script.js') }}"></script>

<script>
(function($) {
    "use strict";

    // Toggle sidebar
    $('#sidebarToggle, #sidebarToggleTop, #topbarSidebarToggle, #topbarSidebarToggleDesktop').on('click', function(e) {
        e.preventDefault();
        $('body').toggleClass('sidebar-toggled');
        $('.sidebar').toggleClass('toggled active');

        if ($('.sidebar').hasClass('toggled')) {
            $('.sidebar .collapse').collapse('hide');
        }
    });

    // Hide collapses on window resize (mobile)
    $(window).on('resize', function() {
        if ($(window).width() < 768) {
            $('.sidebar .collapse').collapse('hide');
        }
    });

    // Sidebar scroll fix for fixed-nav on desktop
    $('body.fixed-nav .sidebar').on('mousewheel DOMMouseScroll wheel', function(e) {
        if ($(window).width() > 768) {
            let e0 = e.originalEvent;
            let delta = e0.wheelDelta || -e0.detail;
            this.scrollTop += (delta < 0 ? 1 : -1) * 30;
            e.preventDefault();
        }
    });

    // Scroll-to-top button show/hide
    $(document).on('scroll', function() {
        if ($(this).scrollTop() > 100) {
            $('.scroll-to-top').fadeIn();
        } else {
            $('.scroll-to-top').fadeOut();
        }
    });

    // Smooth scroll to top
    $(document).on('click', 'a.scroll-to-top', function(e) {
        e.preventDefault();
        $('html, body').stop().animate({
            scrollTop: $($(this).attr('href')).offset().top
        }, 1000, 'easeInOutExpo');
    });

    // Initialize sidebar visibility on page load
    $(document).ready(function() {
        const $sidebar = $('.sidebar');
        const $backdrop = $('#mobileBackdrop');

        function closeSidebarMobile() {
            $sidebar.removeClass('mobile-show');
            $backdrop.removeClass('show');
        }

        function openSidebarMobile() {
            $sidebar.addClass('mobile-show');
            $backdrop.addClass('show');
        }

        // Mobile sidebar toggle
        $('#sidebarToggleTop').on('click', function(e) {
            e.preventDefault();
            $sidebar.hasClass('mobile-show') ? closeSidebarMobile() : openSidebarMobile();
        });

        // Backdrop click
        $backdrop.on('click', function() {
            closeSidebarMobile();
        });

        // Close on outside click
        $(document).on('click', function(e) {
            if ($(window).width() <= 768) {
                if (!$(e.target).closest('.sidebar, #sidebarToggleTop').length && $sidebar.hasClass('mobile-show')) {
                    closeSidebarMobile();
                }
            }
        });

        // Reset on resize
        $(window).on('resize', function() {
            if ($(window).width() > 768) {
                closeSidebarMobile();
            }
        });

        // Close sidebar after link click
        $('.sidebar .nav-link').on('click', function() {
            if ($(window).width() <= 768) {
                setTimeout(() => closeSidebarMobile(), 200);
            }
        });
    });
})(jQuery);
</script>
