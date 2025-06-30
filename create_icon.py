from PIL import Image, ImageDraw
import os


def create_soccer_cam_icon():
    # Create a single high-resolution image
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Calculate dimensions
    padding = size // 10
    circle_size = size - (2 * padding)

    # Draw a blue circle as background
    draw.ellipse(
        (padding, padding, padding + circle_size, padding + circle_size),
        fill=(0, 100, 200, 255),  # Blue fill
        outline=(0, 0, 0, 255),  # Black outline
    )

    # Draw a soccer ball pattern (simplified)
    pentagon_size = circle_size // 5
    positions = [
        (padding + circle_size // 4, padding + circle_size // 4),
        (padding + circle_size // 4 * 3, padding + circle_size // 4),
        (padding + circle_size // 2, padding + circle_size // 2),
        (padding + circle_size // 4, padding + circle_size // 4 * 3),
        (padding + circle_size // 4 * 3, padding + circle_size // 4 * 3),
    ]

    for pos in positions:
        draw.ellipse(
            (
                pos[0] - pentagon_size // 2,
                pos[1] - pentagon_size // 2,
                pos[0] + pentagon_size // 2,
                pos[1] + pentagon_size // 2,
            ),
            fill=(255, 255, 255, 255),  # White fill
        )

    # Draw a camera icon
    camera_size = size // 3
    camera_x = size - camera_size - padding
    camera_y = size - camera_size - padding

    # Camera body
    draw.rectangle(
        (camera_x, camera_y, camera_x + camera_size, camera_y + camera_size),
        fill=(50, 50, 50, 255),  # Dark gray camera
        outline=(0, 0, 0, 255),
    )

    # Camera lens
    lens_size = camera_size // 2
    lens_x = camera_x + (camera_size - lens_size) // 2
    lens_y = camera_y + (camera_size - lens_size) // 2
    draw.ellipse(
        (lens_x, lens_y, lens_x + lens_size, lens_y + lens_size),
        fill=(200, 200, 200, 255),  # Light gray lens
        outline=(0, 0, 0, 255),
    )

    # Save as PNG first
    png_path = os.path.join("video_grouper", "icon.png")
    img.save(png_path)
    print(f"PNG icon saved to {png_path}")

    # Convert to ICO
    icon_path = os.path.join("video_grouper", "icon.ico")

    # Create smaller versions for the ico file
    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for s in sizes:
        resized_img = img.resize((s, s), Image.Resampling.LANCZOS)
        images.append(resized_img)

    # Save as ICO with all sizes
    images[0].save(
        icon_path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[1:]
    )

    print(f"ICO file saved to {icon_path}")


if __name__ == "__main__":
    create_soccer_cam_icon()
