import unittest

from auto_parser.images import (
    avito_image_identity,
    deduplicate_avito_image_urls,
    deduplicate_image_urls,
    has_thumbnail_variants,
    image_identity,
    remap_cached_avito_images,
    remap_cached_images,
)


class ImageTests(unittest.TestCase):
    def test_deduplicates_drom_sizes_and_subdomains(self) -> None:
        small = "https://s12.auto.drom.ru/photo/v2/photo-token/gen272wb.jpg"
        large = "https://s.auto.drom.ru/photo/v2/photo-token/gen1200.jpg"
        second = "https://s1.auto.drom.ru/photo/v2/second-photo/gen600.jpg"

        self.assertEqual(
            deduplicate_image_urls([small, large, second]),
            [large, second],
        )
        self.assertEqual(image_identity(small), image_identity(large))
        self.assertEqual(
            remap_cached_images([large], {small: "cached.jpg"}),
            {large: "cached.jpg"},
        )

    def test_deduplicates_auto_ru_sizes_and_keeps_largest(self) -> None:
        small = "https://avatars.avto.ru/get-autoru-vos/1/photo/320x240"
        large = "https://avatars.avto.ru/get-autoru-vos/1/photo/1200x900"
        second = "https://avatars.avto.ru/get-autoru-vos/2/photo/832x624"
        self.assertEqual(deduplicate_image_urls([small, large, second]), [large, second])
        self.assertEqual(image_identity(small), image_identity(large))
        self.assertEqual(
            remap_cached_images([large], {small: "cached.jpg"}),
            {large: "cached.jpg"},
        )

    def test_keeps_highest_quality_avito_variant_per_photo(self) -> None:
        low = (
            "https://00.img.avito.st/image/1/1."
            "cc48Vra13Scm9wMkUldOzkv33yeC_XMnVvTfJQ.low"
        )
        medium = (
            "https://00.img.avito.st/image/1/1."
            "cc48Vra33Scc9LH9KGQ05Ij13S2CY95LivU.medium"
        )
        high = (
            "https://00.img.avito.st/image/1/1."
            "cc48Vra43ScK_x8iUldOzkv33yGC918vSvLfJY.high"
        )
        another_photo = (
            "https://40.img.avito.st/image/1/1."
            "vQgwRba3C-EQ530jA0nRcYXmEeuOcBKNhuY.other"
        )
        icon = "https://m.avito.ru/icons/touch-icon-512x512.png"

        result = deduplicate_avito_image_urls(
            [low, medium, icon, high, another_photo]
        )

        self.assertEqual(result, [high, another_photo])
        self.assertTrue(has_thumbnail_variants([medium]))
        self.assertFalse(has_thumbnail_variants([high]))

    def test_deduplicates_new_four_variant_format(self) -> None:
        variants = [
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGw1BMZtP6P9.first",
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGxNBL5tP6P9.second",
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGxVAaZoP6P9.third",
            "https://40.img.avito.st/image/1/1."
            "yIUjpLaAZGwlANZpP6P9.fourth",
        ]

        result = deduplicate_avito_image_urls(variants)

        self.assertEqual(result, [variants[0]])
        self.assertEqual(
            avito_image_identity(variants[0]),
            avito_image_identity(variants[3]),
        )

    def test_preserves_cached_file_when_variant_url_changes(self) -> None:
        old = (
            "https://00.img.avito.st/image/1/1."
            "cc48Vra13Scm9wMkUldO.old"
        )
        high = (
            "https://00.img.avito.st/image/1/1."
            "cc48Vra43ScK_x8iUldO.high"
        )

        mapped = remap_cached_avito_images(
            [high],
            {old: "cache/photo.webp"},
        )

        self.assertEqual(mapped, {high: "cache/photo.webp"})


if __name__ == "__main__":
    unittest.main()
