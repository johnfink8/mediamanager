import random

from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session

genres = ["Action", "Comedy", "Drama", "Sci-Fi", "Horror"]
languages = ["English", "French", "Spanish", "German"]
statuses = ["released", "announced", "in production"]
networks = ["HBO", "Netflix", "BBC", "AMC"]
countries = ["USA", "UK", "Canada", "France"]
descriptions = [
    "A hero saves the day.",
    "A villain appears.",
    "A dramatic story unfolds.",
    "A hilarious adventure.",
]


def random_attrs(item_type):
    attrs = {}
    if item_type == "mv":
        attrs["imdb"] = [str(random.randint(1000000, 9999999))]
        attrs["genre"] = [random.choice(genres)]
        attrs["originalLanguage"] = [random.choice(languages)]
        attrs["status"] = [random.choice(statuses)]
        attrs["genres"] = [random.choice(genres)]
        attrs["year"] = [str(random.randint(1980, 2023))]
        attrs["rating"] = [str(round(random.uniform(5.0, 9.5), 1))]
        attrs["country"] = [random.choice(countries)]
        attrs["description"] = [random.choice(descriptions)]
    else:
        attrs["network"] = [random.choice(networks)]
        attrs["genres"] = [random.choice(genres)]
        attrs["status"] = [random.choice(statuses)]
        attrs["year"] = [str(random.randint(1980, 2023))]
        attrs["rating"] = [str(round(random.uniform(5.0, 9.5), 1))]
        attrs["country"] = [random.choice(countries)]
        attrs["description"] = [random.choice(descriptions)]
    return attrs


def main():
    session = db_session()
    # Clear existing test data
    session.query(IgnoreItem).delete()
    session.commit()
    # Add movies
    for i in range(10):
        attrs = random_attrs("mv")
        item = IgnoreItem(
            item_type="mv",
            uid=f"tt{1000000 + i}",
            title=f"Test Movie {i}",
            ignore=False,
            added=False,
            attributes=attrs,
        )
        session.add(item)
    # Add shows
    for i in range(10):
        attrs = random_attrs("tv")
        item = IgnoreItem(
            item_type="tv",
            uid=f"tvdb{i + 1}",
            title=f"Test Show {i}",
            ignore=False,
            added=False,
            attributes=attrs,
        )
        session.add(item)
    session.commit()
    print("Added 10 movies and 10 shows to IgnoreItem table.")


if __name__ == "__main__":
    main()
